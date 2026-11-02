import Foundation

/// WebSocket de estado (/api/v1/ws) com reconexão automática e backoff.
@MainActor
final class StatusSocket {
    var onStatus: ((SystemStatus) -> Void)?
    var onConnectionChange: ((Bool) -> Void)?

    private let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()
    private var task: URLSessionWebSocketTask?
    private var loop: Task<Void, Never>?
    private var baseURL: URL?
    private var token: String?
    private var backoff: Double = 1
    private(set) var connected = false

    func connect(baseURL: URL, token: String) {
        self.baseURL = baseURL
        self.token = token
        restart()
    }

    func disconnect() {
        loop?.cancel()
        loop = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        setConnected(false)
    }

    /// Reconecta imediatamente (ex.: troca de Wi‑Fi para dados móveis).
    func restart() {
        disconnect()
        backoff = 1
        loop = Task { [weak self] in await self?.run() }
    }

    private func setConnected(_ value: Bool) {
        guard connected != value else { return }
        connected = value
        onConnectionChange?(value)
    }

    private func run() async {
        while !Task.isCancelled {
            guard let baseURL, let token, var comps = URLComponents(url: baseURL.appendingPathComponent("api/v1/ws"), resolvingAgainstBaseURL: false) else { return }
            comps.scheme = comps.scheme == "https" ? "wss" : "ws"
            guard let url = comps.url else { return }
            var req = URLRequest(url: url)
            req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
            req.timeoutInterval = 15
            let ws = URLSession.shared.webSocketTask(with: req)
            task = ws
            ws.resume()
            do {
                while !Task.isCancelled {
                    let msg = try await ws.receive()
                    if case .string(let text) = msg, let data = text.data(using: .utf8),
                       let status = try? decoder.decode(StatusMessage.self, from: data) {
                        backoff = 1
                        setConnected(true)
                        onStatus?(status.data)
                    }
                }
            } catch {
                setConnected(false)
            }
            ws.cancel(with: .goingAway, reason: nil)
            if Task.isCancelled { return }
            try? await Task.sleep(nanoseconds: UInt64(backoff * 1_000_000_000))
            backoff = min(backoff * 2, 30)
        }
    }
}
