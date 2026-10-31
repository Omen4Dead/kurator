import SwiftUI

struct GroupCardView: View {
    @EnvironmentObject var model: AppModel
    let group: FindingGroup
    let index: Int

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text("\(index + 1). \(title)")
                        .font(.headline)
                    Text("\(group.size) файла(ов)")
                        .font(.caption)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Color.secondary.opacity(0.2), in: Capsule())
                }
                if !detailText.isEmpty {
                    Text(detailText)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 128), spacing: 8)], spacing: 8) {
                    ForEach(group.files) { file in
                        TileView(group: group, file: file)
                    }
                }
                HStack {
                    let chosen = model.selectedPaths(group)
                    let bytes = group.files.filter { chosen.contains($0.path) }
                        .reduce(Int64(0)) { $0 + ($1.size ?? 0) }
                    Text("перенести: \(chosen.count) из \(group.size)" +
                         (bytes > 0 ? " · \(fmtBytes(bytes))" : ""))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Spacer()
                    Button("В карантин", role: .destructive) {
                        Task { await model.decide(chosen, action: "remove") }
                    }
                    Button("Оставить оба") {
                        Task { await model.decide(group.members, action: "skip", sig: group.sig) }
                    }
                    .buttonStyle(.borderless)
                    Button("Сброс") {
                        model.selected.removeValue(forKey: group.sig)
                    }
                    .buttonStyle(.borderless)
                }
            }
        }
    }

    private var title: String {
        let path = group.keeper ?? group.members.first ?? ""
        return path.split(separator: "/").last.map(String.init) ?? path
    }

    private var detailText: String {
        (group.detail ?? [:])
            .filter { $0.key != "sig" }
            .sorted { $0.key < $1.key }
            .map { "\($0.key)=\($0.value.text)" }
            .joined(separator: ", ")
    }
}

struct TileView: View {
    @EnvironmentObject var model: AppModel
    let group: FindingGroup
    let file: FindingGroup.FileItem

    var body: some View {
        let picked = model.isSelected(group, file.path)
        ZStack(alignment: .topLeading) {
            VStack(spacing: 2) {
                ThumbView(rel: file.path)
                Text(file.path.split(separator: "/").last.map(String.init) ?? file.path)
                    .font(.caption2)
                    .lineLimit(1)
                Text(meta)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
            Button { model.toggle(group, file.path) } label: {
                Image(systemName: picked ? "checkmark.circle.fill" : "circle")
                    .foregroundStyle(picked ? Color.accentColor : Color.secondary)
                    .padding(6)
                    .background(.regularMaterial, in: Circle())
            }
            .buttonStyle(.plain)
            .padding(4)
            .help(picked ? "Снять отметку" : "Отметить для переноса")
        }
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(picked ? Color.red : .clear, lineWidth: 2)
        )
    }

    private var meta: String {
        var parts: [String] = []
        if let taken = file.taken_at, taken.count >= 10 {
            parts.append(String(taken.prefix(10)))
        }
        if let size = file.size { parts.append(fmtBytes(size)) }
        return parts.joined(separator: " · ")
    }
}

/// Полоса «оставлено как есть» вместо карточки пропущенной группы.
struct SkippedStrip: View {
    @EnvironmentObject var model: AppModel
    let group: FindingGroup
    let createdAt: Int?

    var body: some View {
        HStack {
            Text("оставлено как есть")
                .foregroundStyle(.secondary)
            Text(group.keeper ?? group.members.first ?? "")
                .lineLimit(1)
            Text("\(group.size) файла(ов)" +
                 (createdAt.map { " · скрыто \(whenRu($0))" } ?? ""))
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
            Button("Вернуть в разбор") {
                Task { await model.unskip(group.sig) }
            }
            .buttonStyle(.borderless)
        }
        .padding(8)
        .background(Color.secondary.opacity(0.1), in: RoundedRectangle(cornerRadius: 8))
    }
}
