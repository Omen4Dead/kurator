import Foundation

struct AppState: Decodable {
    let root: String
    let files: Int
    let bytes: Int64
    let embedded: Int
    let model: ModelInfo
    let defaults: Defaults
    let factory: Factory
    let skipped: Int
    let skipped_groups: [SkippedGroup]
    let last_index_at: Int?
    let job: Job?

    struct ModelInfo: Decodable {
        let present: Bool
    }
    struct Defaults: Decodable {
        let phash: Double
        let cosine: Double
    }
    struct Factory: Decodable {
        let phash: Double
        let cosine: Double
    }
    struct SkippedGroup: Decodable {
        let sig: String
        let created_at: Int?
    }
}

struct Job: Decodable {
    let status: String
    let progress: String?
    let stats: JobStats?
    let error: String?

    struct JobStats: Decodable {
        let seen: Int?
        let indexed: Int?
        let embedded: Int?
    }
}

struct FindingGroup: Decodable, Identifiable {
    let kind: String
    let size: Int
    let keeper: String?
    let members: [String]
    let detail: [String: JSONValue]?
    let victims: [String]
    let files: [FileItem]

    var id: String { sig }
    var sig: String {
        if case .string(let s)? = detail?["sig"] { return s }
        return "\(kind):\(members.first ?? ""):\(members.count)"
    }

    struct FileItem: Decodable, Identifiable {
        let path: String
        let size: Int64?
        let taken_at: String?
        let role: String?
        var id: String { path }
    }
}

struct Cluster: Decodable, Identifiable {
    let size: Int
    let members: [String]
    var id: String { members.first ?? UUID().uuidString }
}

struct Findings: Decodable {
    let groups: [FindingGroup]
    let clusters: [Cluster]
}

struct TrashItem: Decodable, Identifiable {
    let key: String
    let from_path: String?
    let to_path: String
    let size: Int64
    let orphan: Bool
    let junk: Bool
    var id: String { key }
}

struct TrashResponse: Decodable {
    let items: [TrashItem]
    let count: Int
}

struct RootSuggestion: Decodable, Identifiable {
    let path: String
    let kind: String
    let index: IndexStatus?
    var id: String { path }

    struct IndexStatus: Decodable {
        let files: Int
        let last_index_at: Int?
    }
}

struct RootsResponse: Decodable {
    let suggestions: [RootSuggestion]
    let current: String
}

/// Значение произвольного типа из detail: сервер кладёт туда и строки,
/// и числа, поэтому жёсткий тип не задать.
enum JSONValue: Decodable {
    case string(String)
    case number(Double)
    case bool(Bool)

    init(from decoder: Decoder) throws {
        let box = try decoder.singleValueContainer()
        if let s = try? box.decode(String.self) { self = .string(s); return }
        if let n = try? box.decode(Double.self) { self = .number(n); return }
        if let b = try? box.decode(Bool.self) { self = .bool(b); return }
        self = .string("")
    }

    var text: String {
        switch self {
        case .string(let s): return s
        case .number(let n): return n.truncatingRemainder(dividingBy: 1) == 0
            ? String(Int(n)) : String(format: "%.3f", n)
        case .bool(let b): return b ? "да" : "нет"
        }
    }
}

// Русские числительные: 1 группа, 2 группы, 5 групп.
func plural(_ n: Int, _ one: String, _ few: String, _ many: String) -> String {
    let mod100 = n % 100
    let mod10 = n % 10
    if (11...14).contains(mod100) { return many }
    if mod10 == 1 { return one }
    if (2...4).contains(mod10) { return few }
    return many
}

func fmtBytes(_ n: Int64?) -> String {
    guard let n else { return "—" }
    let units = ["Б", "КБ", "МБ", "ГБ", "ТБ"]
    var value = Double(n)
    var i = 0
    while value >= 1024 && i < units.count - 1 { value /= 1024; i += 1 }
    return String(format: "%.1f %@", value, units[i])
}

func whenRu(_ stamp: Int) -> String {
    let df = DateFormatter()
    df.locale = Locale(identifier: "ru_RU")
    df.dateStyle = .short
    df.timeStyle = .short
    return df.string(from: Date(timeIntervalSince1970: TimeInterval(stamp)))
}
