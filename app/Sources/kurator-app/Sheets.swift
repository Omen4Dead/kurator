import SwiftUI

struct RootPickerView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @ViewState private var suggestions: [RootSuggestion] = []
    @ViewState private var manual = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Папка с фотографиями").font(.headline)
            Text("После смены папки индекс будет построен заново для неё.")
                .font(.callout)
                .foregroundStyle(.secondary)
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 4) {
                    ForEach(suggestions) { sug in
                        Button {
                            Task { await model.switchRoot(sug.path) }
                        } label: {
                            VStack(alignment: .leading) {
                                Text(sug.path)
                                Text(note(for: sug))
                                    .font(.caption)
                                    .foregroundStyle(.secondary)
                            }
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(8)
                            .background(sug.path == model.root
                                        ? Color.accentColor.opacity(0.12) : .clear,
                                        in: RoundedRectangle(cornerRadius: 6))
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
            .frame(minHeight: 200)
            HStack {
                TextField("или вставьте путь к папке", text: $manual)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit { Task { await model.switchRoot(manual) } }
                Button("Перейти") { Task { await model.switchRoot(manual) } }
                    .buttonStyle(.borderedProminent)
            }
            HStack {
                Spacer()
                Button("Отмена") { dismiss() }
            }
        }
        .padding()
        .frame(width: 520, height: 420)
        .task {
            if let api = model.api,
               let data: RootsResponse = try? await api.call("/api/roots") {
                suggestions = data.suggestions
                manual = data.current
            }
        }
    }

    private func note(for sug: RootSuggestion) -> String {
        if let index = sug.index {
            let when = index.last_index_at.map(whenRu) ?? "дата неизвестна"
            return "индекс: \(index.files) файлов · \(when)"
        }
        return "\(sug.kind) · ещё не индексирована"
    }
}

struct TrashView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @ViewState private var items: [TrashItem] = []
    @ViewState private var chosen: Set<String> = []
    @ViewState private var armed: Arm?

    private enum Arm { case chosen, all }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("Корзина").font(.headline)
                Text("\(items.count)")
                    .font(.caption)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(Color.secondary.opacity(0.2), in: Capsule())
            }
            Text("Здесь то, что уже перенесено в карантин. Отметьте файлы и верните их — ничего не удаляется, файлы просто едут обратно. А вот удаление отсюда необратимо.")
                .font(.callout)
                .foregroundStyle(.secondary)
            HStack {
                Button("Выбрать все") {
                    chosen = Set(items.filter { !$0.orphan }.map(\.key))
                    armed = nil
                }
                .buttonStyle(.borderless)
                Button("Снять всё") { chosen.removeAll(); armed = nil }
                    .buttonStyle(.borderless)
                Text("выбрано \(chosen.count)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 4) {
                    ForEach(items) { item in
                        row(item)
                    }
                }
            }
            HStack {
                Button("Закрыть") { dismiss() }
                Spacer()
                Button(armed == .all
                       ? "Точно удалить все \(items.count)? Отменить нельзя"
                       : "Удалить всё", role: .destructive) {
                    if armed == .all { Task { await runDelete(mode: "all") } }
                    else { armed = .all }
                }
                .disabled(items.isEmpty)
                Button("Вернуть выбранные (\(restorableChosen))") {
                    Task { await runRestore() }
                }
                .disabled(restorableChosen == 0)
                Button(armed == .chosen
                       ? "Точно удалить \(chosen.count) файл(ов)? Отменить нельзя"
                       : "Удалить выбранные (\(chosen.count))", role: .destructive) {
                    if armed == .chosen { Task { await runDelete(mode: "chosen") } }
                    else { armed = .chosen }
                }
                .disabled(chosen.isEmpty)
            }
        }
        .padding()
        .frame(width: 620, height: 480)
        .task { await reload() }
    }

    private var restorableChosen: Int {
        let restorable = Set(items.filter { !$0.orphan }.map(\.key))
        return chosen.filter { restorable.contains($0) }.count
    }

    private func row(_ item: TrashItem) -> some View {
        HStack(spacing: 8) {
            Toggle("", isOn: Binding(
                get: { chosen.contains(item.key) },
                set: { on in
                    if on { chosen.insert(item.key) } else { chosen.remove(item.key) }
                    armed = nil
                }
            ))
            .labelsHidden()
            .disabled(item.orphan)
            ThumbView(rel: item.to_path)
                .frame(width: 48, height: 48)
            VStack(alignment: .leading, spacing: 2) {
                Text(item.to_path.split(separator: "/").last.map(String.init) ?? item.to_path)
                    .lineLimit(1)
                Text(item.from_path ?? "неизвестно, манифест потерян")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                if item.junk {
                    Text("служебный файл macOS, не фотография")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                } else if item.orphan {
                    Text("без манифеста — вернуть нечем")
                        .font(.caption2)
                        .foregroundStyle(.orange)
                }
            }
            Spacer()
            Text(fmtBytes(item.size))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }

    private func reload() async {
        guard let api = model.api,
              let data: TrashResponse = try? await api.call("/api/trash") else { return }
        items = data.items
        chosen.removeAll()
        armed = nil
    }

    private func runDelete(mode: String) async {
        guard let api = model.api else { return }
        let keys = mode == "all" ? items.map(\.key) : Array(chosen)
        do {
            let result = try await api.callDict("/api/trash/delete", method: "POST",
                                                json: ["keys": keys, "confirm": true, "mode": mode])
            model.showToast("удалено навсегда \(result["deleted"] ?? 0) (\(fmtBytes((result["bytes"] as? Int64) ?? 0)))")
        } catch {
            model.showToast(error.localizedDescription, bad: true)
        }
        await model.loadTrashCount()
        await reload()
        if items.isEmpty { dismiss() }
    }

    private func runRestore() async {
        guard let api = model.api else { return }
        let restorable = Set(items.filter { !$0.orphan }.map(\.key))
        let keys = chosen.filter { restorable.contains($0) }
        do {
            let result = try await api.callDict("/api/trash/restore", method: "POST",
                                                json: ["keys": keys])
            model.showToast("возвращено на место \(result["restored"] ?? 0) (\(fmtBytes((result["bytes"] as? Int64) ?? 0)))")
        } catch {
            model.showToast(error.localizedDescription, bad: true)
        }
        await model.loadTrashCount()
        await model.loadFindings()
        await model.loadState()
        await reload()
        if items.isEmpty { dismiss() }
    }
}
