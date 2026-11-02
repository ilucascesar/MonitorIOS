import Foundation

enum APIError: LocalizedError, Equatable {
    case notConfigured
    case unauthorized
    case rateLimited(String)
    case server(status: Int, message: String)
    case network(String)
    case decoding(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured: return "Configure o endereço do servidor."
        case .unauthorized: return "Sessão expirada. Entre novamente."
        case .rateLimited(let m): return m
        case .server(_, let m): return m
        case .network(let m): return "Sem conexão com o servidor: \(m)"
        case .decoding(let m): return "Resposta inesperada do servidor (\(m))."
        }
    }
}

/// Cliente HTTP da API. O token de sessão vai no cabeçalho Authorization; URLs de mídia
/// (HLS) já vêm do servidor com um token curto próprio, pois o AVPlayer não envia cabeçalhos.
final class APIClient: @unchecked Sendable {
    private(set) var baseURL: URL?
    private(set) var token: String?
    private let session: URLSession
    private let decoder: JSONDecoder
    var onUnauthorized: (@Sendable () -> Void)?

    init() {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 20
        cfg.timeoutIntervalForResource = 600
        cfg.waitsForConnectivity = false
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.httpMaximumConnectionsPerHost = 6
        session = URLSession(configuration: cfg)
        decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
    }

    func configure(baseURL: URL?, token: String?) {
        self.baseURL = baseURL
        self.token = token
    }

    static func normalize(_ raw: String) -> URL? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        while s.hasSuffix("/") { s.removeLast() }
        if !s.lowercased().hasPrefix("http://") && !s.lowercased().hasPrefix("https://") { s = "http://" + s }
        guard let url = URL(string: s), let host = url.host, !host.isEmpty else { return nil }
        return url
    }

    /// Resolve um caminho relativo devolvido pela API (ex.: "/api/v1/live/1/sub/index.m3u8?mt=...").
    func absoluteURL(_ pathAndQuery: String) -> URL? {
        guard let baseURL else { return nil }
        return URL(string: pathAndQuery, relativeTo: baseURL)?.absoluteURL
    }

    private func makeRequest(_ path: String, query: [URLQueryItem], method: String, body: [String: Any]?) throws -> URLRequest {
        let relative = path.hasPrefix("/") ? String(path.dropFirst()) : path
        guard let baseURL, var comps = URLComponents(url: baseURL.appendingPathComponent(relative), resolvingAgainstBaseURL: false) else {
            throw APIError.notConfigured
        }
        if !query.isEmpty { comps.queryItems = query }
        guard let url = comps.url else { throw APIError.notConfigured }
        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        if let token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return req
    }

    func data(_ path: String, query: [URLQueryItem] = [], method: String = "GET", body: [String: Any]? = nil) async throws -> Data {
        let req = try makeRequest(path, query: query, method: method, body: body)
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch let e as URLError {
            throw APIError.network(e.localizedDescription)
        }
        guard let http = response as? HTTPURLResponse else { throw APIError.network("resposta inválida") }
        switch http.statusCode {
        case 200..<300:
            return data
        case 401:
            onUnauthorized?()
            throw APIError.unauthorized
        case 429:
            throw APIError.rateLimited(Self.detail(data) ?? "Muitas tentativas. Aguarde.")
        default:
            throw APIError.server(status: http.statusCode, message: Self.detail(data) ?? "Erro \(http.statusCode)")
        }
    }

    func send<T: Decodable>(_ type: T.Type, _ path: String, query: [URLQueryItem] = [], method: String = "GET",
                            body: [String: Any]? = nil) async throws -> T {
        let data = try await data(path, query: query, method: method, body: body)
        do {
            return try decoder.decode(T.self, from: data)
        } catch {
            throw APIError.decoding(String(describing: error).prefix(160).description)
        }
    }

    func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        try decoder.decode(T.self, from: data)
    }

    /// Baixa um arquivo grande para uma pasta temporária, informando o progresso (0...1).
    func download(_ url: URL, suggestedName: String, progress: @escaping @Sendable (Double) -> Void) async throws -> URL {
        let dest = FileManager.default.temporaryDirectory.appendingPathComponent(suggestedName)
        let delegate = DownloadDelegate(destination: dest, progress: progress)
        let dlSession = URLSession(configuration: .default, delegate: delegate, delegateQueue: nil)
        defer { dlSession.finishTasksAndInvalidate() }
        return try await withCheckedThrowingContinuation { cont in
            delegate.continuation = cont
            dlSession.downloadTask(with: url).resume()
        }
    }

    private static func detail(_ data: Data) -> String? {
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        if let s = obj["detail"] as? String { return s }
        if let arr = obj["detail"] as? [[String: Any]], let first = arr.first, let msg = first["msg"] as? String { return msg }
        return nil
    }
}
