import SwiftUI

struct ContentView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            toolbar
            if model.jobRunning {
                ProgressView()
                    .progressViewStyle(.linear)
                Text(model.job?.progress ?? "запускаю…")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal)
                    .padding(.bottom, 4)
            }
            Divider()
            tabs
            Divider()
            main
        }
        .overlay(alignment: .bottom) { toastView }
        .sheet(isPresented: $model.showPicker) { RootPickerView() }
        .sheet(isPresented: $model.showTrash) { TrashView() }
        .alert("Пересчитать всё заново", isPresented: $model.showRehash) {
            Button("Отмена", role: .cancel) {}
            Button("Начать заново", role: .destructive) {
                Task { await model.startIndex(rehash: true) }
            }
        } message: {
            Text("\(model.rehashCost)\nПридётся заново посчитать хеши и эмбеддинги для всех файлов. Индекс останется на месте, но процесс небыстрый.")
        }
    }

    private var header: some View {
        HStack {
            Text("kurator").font(.headline)
            Button { model.showPicker = true } label: {
                HStack(spacing: 4) {
                    Text(model.root.isEmpty ? "…" : model.root)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Text("сменить").foregroundStyle(.secondary)
                }
            }
            .buttonStyle(.borderless)
            .help("Сменить папку с фотографиями")
            // Автоматическая подпись кнопки из вложенных Text считается
            // один раз и не перечитывается при смене корня: сменил папку,
            // а accessibility-дерево вечно показывает старую. Явная
            // подпись пересчитывается вместе с model.root.
            .accessibilityLabel("\(model.root.isEmpty ? "…" : model.root), сменить")
            Spacer()
            Button("Отчёт") { Task { await model.makeReport() } }
                .buttonStyle(.borderless)
            Button("Выйти") { model.shutdown(); NSApp.terminate(nil) }
                .buttonStyle(.borderless)
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
    }

    private var toolbar: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button("Обновить индекс") { Task { await model.startIndex() } }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.jobRunning || model.api == nil)
                Button("пересчитать всё заново") {
                    Task {
                        // Стоимость показываем до подтверждения: число
                        // файлов известно заранее, оценка — 12 файлов/с.
                        model.rehashCost = ""
                        model.showRehash = true
                    }
                }
                .buttonStyle(.borderless)
                .disabled(model.jobRunning || model.api == nil)
                Text(model.indexNote)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            HStack {
                tuningField("Похожесть кадра", text: $model.phash,
                            hint: "Сравнивается сам кадр: свет, тень, резкость, расположение объектов. Это расстояние между хешами — чем меньше число, тем строже отбор: уменьшишь, и в «Похожие» попадут только почти одинаковые кадры; увеличишь — добавятся кадры с той же композицией при другом свете. Заводское значение — 4.")
                tuningField("Похожесть содержания", text: $model.cosine,
                            hint: "Сравнивается, что изображено — сюжет кадра. Это сходство по смыслу — чем больше число, тем строже: увеличишь, и рядом окажутся только кадры с одинаковым содержимым; уменьшишь — добавятся разные кадры похожей сцены. Если непонятно — оставьте 0.9.")
                Button("сбросить") { Task { await model.resetFactory() } }
                    .buttonStyle(.borderless)
                Text("Оба меняют строгость, но в разные стороны")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                Spacer()
                if !model.skipped.isEmpty {
                    Button("Вернуть пропущенные (\(model.skipped.count))") {
                        Task { await model.decide(["-"], action: "clear") }
                    }
                }
                if model.trashCount > 0 {
                    Button("Вернуть из карантина (\(model.trashCount))") {
                        Task { await model.undoAll() }
                    }
                    Button("Корзина (\(model.trashCount))") { model.showTrash = true }
                }
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 6)
    }

    private func tuningField(_ label: String, text: Binding<String>, hint: String) -> some View {
        HStack(spacing: 4) {
            Text(label).font(.callout)
            TextField("", text: text)
                .frame(width: 48)
                .textFieldStyle(.roundedBorder)
                .onSubmit {
                    Task {
                        await model.saveTuning()
                        await model.loadFindings()
                    }
                }
            // Нативный .help на macOS показывает подсказку нестабильно
            // (и не кликабелен) — делаем явную кнопку с поповером.
            HintButton(hint: hint)
        }
    }

    private var tabs: some View {
        HStack(spacing: 0) {
            ForEach(AppModel.Tab.allCases, id: \.self) { tab in
                Button(tab.title) { model.tab = tab }
                    .buttonStyle(.borderless)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 6)
                    .background(model.tab == tab ? Color.accentColor.opacity(0.15) : .clear)
            }
            Spacer()
            // Кнопка страницы — в строке вкладок: в списке она уезжала
            // при прокрутке, а разбирают архив именно сверху вниз.
            if model.tab != .clusters {
                Button("Отправить страницу в карантин (\(pageChosen.count))", role: .destructive) {
                    Task { await model.removePaths(pageChosen) }
                }
                .disabled(pageChosen.isEmpty)
                .help("Переносит в карантин ровно те кадры, что отмечены на этой странице")
            }
        }
        .padding(.horizontal)
    }

    /// Раскладка текущей вкладки на страницы — общая для кнопки в
    /// строке вкладок и списка ниже: считаем один раз, чтобы оба видели
    /// одну и ту же десятку. Пропущенные группы в раскладку не входят:
    /// после «Оставить оба» группа исчезает со страницы, назад её
    /// возвращает только «Вернуть пропущенные» сверху.
    private var pagedGroups: (all: [FindingGroup], skipped: Int, slice: [FindingGroup], pageCount: Int, page: Int) {
        let kindGroups = model.groups.filter { $0.kind == model.tab.rawValue }
        let all = kindGroups.filter { model.skipped[$0.sig] == nil }
        let perPage = AppModel.groupsPerPage
        let pageCount = max(1, (all.count + perPage - 1) / perPage)
        let page = min(model.page(for: model.tab), pageCount - 1)
        let slice = Array(all[page * perPage ..< min(all.count, (page + 1) * perPage)])
        return (all, kindGroups.count - all.count, slice, pageCount, page)
    }

    private var pageChosen: [String] {
        pagedGroups.slice.flatMap { model.selectedPaths($0) }
    }

    @ViewBuilder
    private var main: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                switch model.tab {
                case .clusters:
                    clustersContent
                case .near, .exact:
                    groupsContent
                }
            }
            .padding()
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    @ViewBuilder
    private var clustersContent: some View {
        if model.clusters.isEmpty {
            Text("Кластеров нет. Понизь порог cosine или проиндексируй больше.")
                .foregroundStyle(.secondary)
        } else {
            ForEach(Array(model.clusters.enumerated()), id: \.element.id) { i, cluster in
                GroupBox {
                    Text("\(i + 1). кластер · \(cluster.size) файла(ов)")
                        .font(.headline)
                    Text(cluster.members.first.map {
                        $0.split(separator: "/").dropLast().joined(separator: "/")
                    } ?? "")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    LazyVGrid(columns: tileColumns, spacing: 8) {
                        ForEach(cluster.members, id: \.self) { member in
                            VStack {
                                ThumbView(rel: member)
                                Text(member.split(separator: "/").last.map(String.init) ?? member)
                                    .font(.caption2)
                                    .lineLimit(1)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder
    private var groupsContent: some View {
        let kind = model.tab.rawValue
        let all = pagedGroups.all
        if all.isEmpty {
            let anyAtAll = model.groups.contains { $0.kind == kind }
            Text(anyAtAll
                 ? "Все группы этой вкладки оставлены как есть. Вернуть их можно кнопкой «Вернуть пропущенные» в панели сверху."
                 : "Здесь пусто. Проверь индекс или открой другую вкладку.")
                .foregroundStyle(.secondary)
        } else {
            // Страницы по 10 групп: сотни карточек с превью разом едят
            // память, а разбирают архив всё равно по одной группе.
            let perPage = AppModel.groupsPerPage
            let pageCount = pagedGroups.pageCount
            let page = pagedGroups.page
            let slice = pagedGroups.slice
            let skipped = pagedGroups.skipped
            let total = all.reduce(Int64(0)) { sum, g in
                sum + g.files.filter { model.isSelected(g, $0.path) }
                    .reduce(Int64(0)) { $0 + ($1.size ?? 0) }
            }
            let headline = "\(all.count) \(plural(all.count, "группа", "группы", "групп")) в работе" +
                  (skipped > 0 ? ", ещё \(skipped) оставлены как есть" : "") +
                  " · отмечено на \(fmtBytes(total))"
            Text(headline + ".")
                .font(.callout)
                .foregroundStyle(.secondary)
            Text("Красная рамка — уедет в карантин, без рамки останется. Чтобы оставить другой кадр, снимите ✓ здесь и поставьте там.")
                .font(.caption)
                .foregroundStyle(.secondary)
            if pageCount > 1 {
                HStack {
                    Button("←") { model.setPage(model.tab, page - 1) }
                        .disabled(page == 0)
                    Text("Страница \(page + 1) из \(pageCount)")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Button("→") { model.setPage(model.tab, page + 1) }
                        .disabled(page >= pageCount - 1)
                    Spacer()
                }
            }
            // Пропущенных здесь быть не может: pagedGroups их отфильтровал,
            // и страница всегда состоит из живых групп.
            ForEach(Array(slice.enumerated()), id: \.element.id) { i, group in
                GroupCardView(group: group, index: page * perPage + i)
            }
            if pageCount > 1 {
                HStack {
                    Button("←") { model.setPage(model.tab, page - 1) }
                        .disabled(page == 0)
                    Text("Страница \(page + 1) из \(pageCount)")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Button("→") { model.setPage(model.tab, page + 1) }
                        .disabled(page >= pageCount - 1)
                    Spacer()
                }
            }
        }
    }

    private var tileColumns: [GridItem] {
        [GridItem(.adaptive(minimum: thumbSide), spacing: 8)]
    }

    @ViewBuilder
    private var toastView: some View {
        if let toast = model.toast {
            Text(toast.text)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(toast.bad ? Color.red.opacity(0.9) : Color.primary.opacity(0.85),
                            in: Capsule())
                .foregroundStyle(toast.bad ? .white : Color(nsColor: .windowBackgroundColor))
                .padding(.bottom, 16)
        }
    }
}

/// Сторона превью в точках: было 128, подняли до 141 (+10%), сейчас
/// 162 (+15% сверх того). Ширина запроса к серверу поднята
/// пропорционально — иначе на Retina мыло.
let thumbSide: CGFloat = 162

/// Знак вопроса у параметра: клик открывает поповер с пояснением,
/// что меняет параметр и куда крутить число.
struct HintButton: View {
    let hint: String
    @State private var shown = false

    var body: some View {
        Button { shown.toggle() } label: {
            Image(systemName: "questionmark.circle")
                .foregroundStyle(.secondary)
        }
        .buttonStyle(.plain)
        .popover(isPresented: $shown, arrowEdge: .bottom) {
            Text(hint)
                .font(.callout)
                .frame(maxWidth: 320)
                .padding(12)
        }
        .accessibilityLabel("Пояснение: \(hint)")
        .help("Показать пояснение")
    }
}

/// Превью с сервера. AsyncImage сам кеширует, токен — в адресе.
struct ThumbView: View {
    @EnvironmentObject var model: AppModel
    let rel: String
    var width: Int = 324

    var body: some View {
        if let api = model.api {
            AsyncImage(url: api.thumbURL(rel, width: width)) { phase in
                switch phase {
                case .success(let image):
                    image.resizable().aspectRatio(contentMode: .fill)
                default:
                    Color.secondary.opacity(0.15)
                }
            }
            .frame(width: thumbSide, height: thumbSide)
            .clipped()
            .cornerRadius(6)
            .contextMenu {
                Button("Показать в Finder") { model.revealInFinder(rel) }
            }
        }
    }
}
