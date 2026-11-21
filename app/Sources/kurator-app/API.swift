import Foundation

/// Управление python-бэкендом: приложение поднимает его процессом и
/// гасит при выходе. Никакого файла экземпляра: приложение само выбирает
/// порт и токен и передаёт их бэкенду переменными окружения.
final class Backend {
    private var process: Process?
    private(set) var baseURL: URL?
    private(set) var token: String = ""

    /// Путь до исполняемого бэкенда: собранный kurator внутри .app,
    /// либо `kurator` из окружения при разработке.
    private func backendCommand() -> String {
        let env = ProcessInfo.processInfo.environment
        if let custom = env["KURATOR_BACKEND"], !custom.isEmpty {
            return custom
        }
        let bundled = Bundle.main.bundleURL
            .appendingPathComponent("Contents/Resources/backend/kurator").path
        if FileManager.default.isExecutableFile(atPath: bundled) {
            return bundled
        }
        // Разработка: репозиторий с uv-окружением.
        if let repo = env["KURATOR_REPO"], !repo.isEmpty {
            return "\(repo)/.venv/bin/kurator"
        }
        return ""
    }

    /// Свободный порт: спрашиваем у системы и сразу отпускаем. Между
    /// этим и стартом бэкенда порт теоретически может занять другой
    /// процесс — тогда бэкенд не поднимется, и мы скажем об этом честно.
    private func freePort() throws -> Int {
        let sock = socket(AF_INET, SOCK_STREAM, 0)
        guard sock >= 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        defer { close(sock) }
        var addr = sockaddr_in()
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var len = socklen_t(MemoryLayout<sockaddr_in>.size)
        let result = withUnsafeMutablePointer(to: &addr) { ptr -> (Int32, Int32) in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sa in
                let bound = bind(sock, sa, len)
                let named = bound == 0 ? getsockname(sock, sa, &len) : -1
                return (bound, named)
            }
        }
        guard result.0 == 0, result.1 == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        return Int(UInt16(bigEndian: addr.sin_port))
    }

    private func makeToken() -> String {
        var bytes = [UInt8](repeating: 0, count: 24)
        _ = SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes)
        return Data(bytes).base64EncodedString()
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "=", with: "")
    }

    func start() async throws {
        if baseURL != nil { return }
        let port = try freePort()
        let token = makeToken()
        let command = backendCommand()
        let proc = Process()
        var env = ProcessInfo.processInfo.environment
        env["KURATOR_TOKEN"] = token
        proc.environment = env
        if command.isEmpty {
            // Последний шанс — kurator из PATH.
            proc.executableURL = URL(fileURLWithPath: "/usr/bin/env")
            proc.arguments = ["kurator", "ui", "--port", String(port)]
        } else if command.contains(" ") {
            proc.executableURL = URL(fileURLWithPath: "/bin/zsh")
            proc.arguments = ["-c", "exec \(command) ui --port \(port)"]
        } else {
            proc.executableURL = URL(fileURLWithPath: command)
            proc.arguments = ["ui", "--port", String(port)]
        }
        // Вывод бэкенда пишем в файл: у оконного приложения stdout никуда
        // не ведёт, и без этого файла падение бэкенда выглядит как
        // «не поднялся» без единого слова о причине.
        let fm = FileManager.default
        let logURL = fm.homeDirectoryForCurrentUser
            .appendingPathComponent(".kurator/backend.log")
        try? fm.createDirectory(at: logURL.deletingLastPathComponent(),
                                withIntermediateDirectories: true)
        if !fm.fileExists(atPath: logURL.path) {
            fm.createFile(atPath: logURL.path, contents: nil)
        }
        if let log = try? FileHandle(forWritingTo: logURL) {
            log.seekToEndOfFile()
            proc.standardOutput = log
            proc.standardError = log
        }
        do {
            try proc.run()
        } catch {
            throw NSError(domain: "kurator", code: 3, userInfo: [
                NSLocalizedDescriptionKey:
                    "бэкенд не запустился (\(command.isEmpty ? "kurator из PATH" : command)): \(error.localizedDescription)"
            ])
        }
        process = proc
        try await waitForServer(port: port, token: token)
        self.baseURL = URL(string: "http://127.0.0.1:\(port)")!
        self.token = token
    }

    private func waitForServer(port: Int, token: String) async throws {
        // Первый старт бэкенда занимает секунды: распаковка бандла,
        // импорт библиотек. Ждём терпеливо, но не вечно.
        let url = URL(string: "http://127.0.0.1:\(port)/api/state")!
        var req = URLRequest(url: url)
        req.setValue(token, forHTTPHeaderField: "X-Kurator-Token")
        req.timeoutInterval = 2
        for _ in 0..<600 {
            if let proc = process, !proc.isRunning {
                throw NSError(domain: "kurator", code: 1, userInfo: [
                    NSLocalizedDescriptionKey: "бэкенд завершился сразу после запуска"
                ])
            }
            if let (_, resp) = try? await URLSession.shared.data(for: req),
               (resp as? HTTPURLResponse)?.statusCode == 200 {
                return
            }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        throw NSError(domain: "kurator", code: 2, userInfo: [
            NSLocalizedDescriptionKey: "бэкенд не поднялся за две минуты"
        ])
    }

    func stop() {
        guard let proc = process, proc.isRunning else { return }
        proc.terminate()
        process = nil
    }
}

enum APIError: LocalizedError {
    case message(String)
    case offline

    var errorDescription: String? {
        switch self {
        case .message(let text): return text
        case .offline: return "kurator больше не отвечает"
        }
    }
}

/// Тонкий клиент поверх URLSession. Токен уходит в заголовке.
struct API {
    let baseURL: URL
    let token: String

    private func request(_ path: String, method: String = "GET",
                         query: [URLQueryItem] = [], body: Data? = nil) -> URLRequest {
        var comps = URLComponents(url: baseURL.appendingPathComponent(path),
                                  resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comps.queryItems = query }
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        req.setValue(token, forHTTPHeaderField: "X-Kurator-Token")
        if let body {
            req.httpBody = body
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return req
    }

    func call<T: Decodable>(_ path: String, method: String = "GET",
                            query: [URLQueryItem] = [],
                            json: [String: Any]? = nil) async throws -> T {
        let data = try await raw(path, method: method, query: query, json: json)
        return try JSONDecoder().decode(T.self, from: data)
    }

    /// Ответы, у которых нет устойчивой формы (результаты действий),
    /// разбираем словарём.
    func callDict(_ path: String, method: String = "GET",
                  query: [URLQueryItem] = [],
                  json: [String: Any]? = nil) async throws -> [String: Any] {
        let data = try await raw(path, method: method, query: query, json: json)
        return (try? JSONSerialization.jsonObject(with: data) as? [String: Any]) ?? [:]
    }

    private func raw(_ path: String, method: String,
                     query: [URLQueryItem], json: [String: Any]?) async throws -> Data {
        // Ошибка кодирования не должна ронять приложение: NSException из
        // JSONSerialization ловится только здесь (0.24 — падал возврат
        // из корзины на Swift-Set в теле запроса).
        let body: Data?
        do {
            body = try json.map { try JSONSerialization.data(withJSONObject: $0) }
        } catch {
            throw APIError.message("не удалось собрать запрос: \(error.localizedDescription)")
        }
        let req = request(path, method: method, query: query, body: body)
        let data: Data, resp: URLResponse
        do {
            (data, resp) = try await URLSession.shared.data(for: req)
        } catch {
            throw APIError.offline
        }
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        if code == 403 { throw APIError.message("нет токена") }
        if let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let error = payload["error"] as? String, code >= 400 {
            throw APIError.message(error)
        }
        if code >= 400 { throw APIError.message("сервер ответил \(code)") }
        return data
    }

    /// Адрес превью с токеном в строке: AsyncImage заголовков не ставит.
    func thumbURL(_ rel: String, width: Int = 256) -> URL {
        var comps = URLComponents(url: baseURL.appendingPathComponent("/api/thumb"),
                                  resolvingAgainstBaseURL: false)!
        comps.queryItems = [
            URLQueryItem(name: "token", value: token),
            URLQueryItem(name: "path", value: rel),
            URLQueryItem(name: "w", value: String(width)),
        ]
        return comps.url!
    }
}
