import Foundation
import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    let backend = Backend()
    private(set) var api: API?

    @Published var root = ""
    @Published var stats = "загружаю…"
    @Published var indexNote = "—"
    @Published var phash = ""
    @Published var cosine = ""
    @Published var factory: (phash: Double, cosine: Double)?

    @Published var tab: Tab = .near
    @Published var groups: [FindingGroup] = []
    @Published var clusters: [Cluster] = []
    @Published var skipped: [String: Int?] = [:]
    @Published var selected: [String: Set<String>] = [:]

    @Published var job: Job?
    @Published var trashCount = 0
    @Published var toast: (text: String, bad: Bool)?
    @Published var showPicker = false
    @Published var showTrash = false
    @Published var showRehash = false
    @Published var rehashCost = ""
    @Published var error: String?
    /// Номер страницы по вкладке («Похожие» и «Точные дубли» листаются
    /// по 10 групп — иначе окно рендерит сотни превью разом).
    @Published var pages: [String: Int] = [:]

    static let groupsPerPage = 10

    func page(for tab: Tab) -> Int { pages[tab.rawValue] ?? 0 }

    func setPage(_ tab: Tab, _ n: Int) {
        pages[tab.rawValue] = max(0, n)
    }

    private var jobTask: Task<Void, Never>?
    private var toastTask: Task<Void, Never>?

    enum Tab: String, CaseIterable {
        case near = "near-strong"
        case clusters
        case exact

        var title: String {
            switch self {
            case .near: return "Похожие"
            case .clusters: return "Кластеры"
            case .exact: return "Точные дубли"
            }
        }
    }

    func start() async {
        do {
            try await backend.start()
            guard let base = backend.baseURL else { return }
            api = API(baseURL: base, token: backend.token)
            await loadState()
            await loadFindings()
            await loadTrashCount()
        } catch {
            // У оконного приложения нет консоли: дублируем в stderr, чтобы
            // запуск из терминала показал причину, а в окне текст и так есть.
            FileHandle.standardError.write(Data("kurator: \(error.localizedDescription)\n".utf8))
            self.error = error.localizedDescription
        }
    }

    func shutdown() {
        // Сначала вежливо просим сервер остановиться — он снимет след
        // экземпляра. Потом на всякий случай гасим процесс. Вызывается из
        // applicationWillTerminate, где уже нельзя ждать Task: поэтому
        // запрос уходит синхронно с коротким таймаутом.
        if let api {
            var req = URLRequest(url: api.baseURL.appendingPathComponent("/api/quit"))
            req.httpMethod = "POST"
            req.setValue(api.token, forHTTPHeaderField: "X-Kurator-Token")
            req.httpBody = Data("{}".utf8)
            req.timeoutInterval = 1
            let sem = DispatchSemaphore(value: 0)
            URLSession.shared.dataTask(with: req) { _, _, _ in sem.signal() }.resume()
            _ = sem.wait(timeout: .now() + 1.2)
        }
        backend.stop()
    }

    // MARK: - загрузка

    func loadState() async {
        guard let api else { return }
        do {
            let s: AppState = try await api.call("/api/state")
            root = s.root
            stats = "\(s.files) файлов · \(fmtBytes(s.bytes)) · эмбеддингов \(s.embedded)/\(s.files) · " +
                "модель \(s.model.present ? "на месте" : "нет (только хеши)")" +
                (s.skipped > 0 ? " · пропущено \(s.skipped)" : "")
            if phash.isEmpty { phash = String(format: "%g", s.defaults.phash) }
            if cosine.isEmpty { cosine = String(format: "%g", s.defaults.cosine) }
            indexNote = "последний проход: " + (s.last_index_at.map(whenRu) ?? "ни разу")
            factory = (s.factory.phash, s.factory.cosine)
            skipped = Dictionary(uniqueKeysWithValues: s.skipped_groups.map { ($0.sig, $0.created_at) })
            if s.job?.status == "running" { pollJob() }
        } catch {
            showToast(error.localizedDescription, bad: true)
        }
    }

    func loadFindings() async {
        guard let api else { return }
        do {
            let data: Findings = try await api.call("/api/findings", query: [
                URLQueryItem(name: "phash", value: phash),
                URLQueryItem(name: "cosine", value: cosine),
            ])
            groups = data.groups
            clusters = data.clusters
            selected.removeAll()
        } catch {
            showToast(error.localizedDescription, bad: true)
        }
    }

    func loadTrashCount() async {
        guard let api else { return }
        if let data: TrashResponse = try? await api.call("/api/trash") {
            trashCount = data.count
        }
    }

    // MARK: - индексация

    func startIndex(rehash: Bool = false) async {
        guard let api else { return }
        do {
            let _: Job = try await api.call("/api/index", method: "POST",
                                            json: ["rehash": rehash])
            pollJob()
        } catch {
            showToast(error.localizedDescription, bad: true)
        }
    }

    func pollJob() {
        jobTask?.cancel()
        jobTask = Task {
            while !Task.isCancelled {
                guard let api else { return }
                do {
                    let j: Job = try await api.call("/api/job")
                    job = j
                    if j.status == "running" || j.status == "idle" && j.progress != nil {
                        try await Task.sleep(nanoseconds: 700_000_000)
                        continue
                    }
                    if j.status == "error" {
                        showToast("ошибка: \(j.error ?? "?")", bad: true)
                    } else if let s = j.stats, (s.indexed ?? 0) > 0 {
                        showToast("готово: просмотрено \(s.seen ?? 0), новых \(s.indexed ?? 0), эмбеддингов \(s.embedded ?? 0)")
                    } else {
                        showToast("новых и изменённых файлов нет — индекс уже актуален")
                    }
                } catch {
                    showToast(error.localizedDescription, bad: true)
                }
                job = nil
                await loadState()
                await loadFindings()
                return
            }
        }
    }

    var jobRunning: Bool { job?.status == "running" }

    // MARK: - пороги

    func saveTuning() async {
        guard let api,
              let p = Double(phash), let c = Double(cosine) else { return }
        do {
            let _: [String: Bool] = try await api.call("/api/tuning", method: "POST",
                                                       json: ["phash": p, "cosine": c])
            showToast("пороги применены — переиндексация не нужна")
        } catch {
            showToast(error.localizedDescription, bad: true)
        }
    }

    func resetFactory() async {
        guard let f = factory else { return }
        phash = String(format: "%g", f.phash)
        cosine = String(format: "%g", f.cosine)
        await saveTuning()
        await loadFindings()
        showToast("сброшено к \(phash) и \(cosine)")
    }

    // MARK: - решения по группам

    /// Всё отмеченное на странице — в карантин. Семантика галочек не
    /// меняется: у каждой группы по-прежнему можно снять все отметки или
    /// отметить оба кадра, переносится ровно то, что отмечено.
    func removePageSelection(_ pageGroups: [FindingGroup]) async {
        let paths = pageGroups.flatMap { selectedPaths($0) }
        await decide(paths, action: "remove")
    }

    func isSelected(_ group: FindingGroup, _ path: String) -> Bool {
        if let chosen = selected[group.sig] { return chosen.contains(path) }
        return group.files.contains { $0.path == path && $0.role != "keeper" }
    }

    func selectedPaths(_ group: FindingGroup) -> [String] {
        if let chosen = selected[group.sig] {
            return group.files.map(\.path).filter { chosen.contains($0) }
        }
        return group.files.filter { $0.role != "keeper" }.map(\.path)
    }

    func toggle(_ group: FindingGroup, _ path: String) {
        var current = Set(selectedPaths(group))
        if current.contains(path) { current.remove(path) } else { current.insert(path) }
        selected[group.sig] = current
        objectWillChange.send()
    }

    func decide(_ paths: [String], action: String, sig: String = "") async {
        guard let api, !paths.isEmpty || action == "clear" else {
            showToast("ничего не отмечено")
            return
        }
        do {
            let result = try await api.callDict("/api/decide", method: "POST",
                                                json: ["paths": paths, "action": action, "sig": sig])
            if let blocked = result["blocked"] as? [String], !blocked.isEmpty {
                showToast("burst-кадры не трогаю: \(blocked.count) пропущено", bad: true)
            } else if action == "remove" {
                showToast("перенесено \(result["moved"] ?? 0). Вернуть можно в корзине.")
            } else if action == "clear" {
                showToast("вернулось пропущенных: \(result["cleared"] ?? 0)")
            }
            await loadState()
            await loadTrashCount()
            await loadFindings()
        } catch {
            showToast(error.localizedDescription, bad: true)
        }
    }

    func unskip(_ sig: String) async {
        guard let api else { return }
        _ = try? await api.callDict("/api/decide", method: "POST",
                                json: ["action": "unskip", "paths": [String](), "sig": sig])
        showToast("группа вернулась в разбор")
        await loadState()
    }

    // MARK: - корень

    func switchRoot(_ path: String) async {
        guard let api, !path.isEmpty else { return }
        do {
            _ = try await api.callDict("/api/root", method: "POST",
                                       json: ["path": path])
            showPicker = false
            showToast("папка сменена, запускаю индексацию")
            await loadState()
            pollJob()
        } catch {
            showToast(error.localizedDescription, bad: true)
        }
    }

    // MARK: - прочее

    /// Показать файл в Finder — единственный способ из приложения дойти
    /// до оригинала руками.
    func revealInFinder(_ rel: String) {
        guard !root.isEmpty else { return }
        let url = URL(fileURLWithPath: root).appendingPathComponent(rel)
        NSWorkspace.shared.activateFileViewerSelecting([url])
    }

    func makeReport() async {
        guard let api,
              let p = Double(phash), let c = Double(cosine) else { return }
        do {
            let info = try await api.callDict("/api/report", method: "POST",
                                              json: ["phash": p, "cosine": c])
            showToast("отчёт готов: кандидатов \(info["reclaimable_count"] ?? 0)")
            NSWorkspace.shared.open(URL(fileURLWithPath: root)
                .appendingPathComponent("out/report.html"))
        } catch {
            showToast(error.localizedDescription, bad: true)
        }
    }

    func undoAll() async {
        guard let api else { return }
        do {
            let result = try await api.callDict("/api/undo", method: "POST", json: [:])
            showToast(result["message"] as? String ?? "возвращено \(result["moved"] ?? 0)")
            await loadState()
            await loadTrashCount()
            await loadFindings()
        } catch {
            showToast(error.localizedDescription, bad: true)
        }
    }

    func showToast(_ text: String, bad: Bool = false) {
        toast = (text, bad)
        toastTask?.cancel()
        toastTask = Task {
            try? await Task.sleep(nanoseconds: bad ? 6_000_000_000 : 2_600_000_000)
            if !Task.isCancelled { toast = nil }
        }
    }
}
