import Foundation
import SwiftUI
import UIKit

enum ServerConnection: Equatable {
    case unknown, connecting, online, offline(String)

    var label: String {
        switch self {
        case .unknown: return "—"
        case .connecting: return "Conectando…"
        case .online: return "Servidor online"
        case .offline(let why): return why.isEmpty ? "Servidor offline" : why
        }
    }
}

/// Estado global: servidor, sessão, câmeras e estado em tempo real.
@MainActor
final class AppModel: ObservableObject {
    static let defaultServer = "http://100.113.162.101:8765"
    private static let serverKey = "serverURL"
    private static let themeKey = "theme"
    private static let tokenAccount = "session-token"

    @Published var serverURLString: String
    @Published private(set) var user: User?
    @Published private(set) var isLoggedIn = false
    @Published private(set) var cameras: [Camera] = []
    @Published private(set) var status: SystemStatus?
    @Published private(set) var connection: ServerConnection = .unknown
    @Published var theme: String { didSet { UserDefaults.standard.set(theme, forKey: Self.themeKey) } }

    let api = APIClient()
    let network = NetworkMonitor()
    private let socket = StatusSocket()
    private var pollTask: Task<Void, Never>?

    var colorScheme: ColorScheme? { theme == "system" ? nil : (theme == "light" ? .light : .dark) }

    init() {
        serverURLString = UserDefaults.standard.string(forKey: Self.serverKey) ?? Self.defaultServer
        theme = UserDefaults.standard.string(forKey: Self.themeKey) ?? "dark"
        socket.onStatus = { [weak self] st in
            self?.status = st
            self?.connection = .online
        }
        socket.onConnectionChange = { [weak self] up in
            guard let self else { return }
            if up { self.connection = .online } else if self.isLoggedIn { Task { await self.refreshStatus() } }
        }
        api.onUnauthorized = { [weak self] in
            Task { @MainActor in self?.handleUnauthorized() }
        }
        network.onChange = { [weak self] in
            // Troca de rede: reabre o WebSocket e atualiza o estado imediatamente.
            guard let self, self.isLoggedIn else { return }
            self.socket.restart()
            Task { await self.refreshAll() }
        }
    }

    func start() {
        network.start()
        let token = KeychainStore.get(Self.tokenAccount)
        api.configure(baseURL: APIClient.normalize(serverURLString), token: token)
        guard token != nil else { return }
        isLoggedIn = true
        Task {
            if let me = try? await api.send(User.self, "/api/v1/auth/me") { user = me }
            await refreshAll()
            connectRealtime()
        }
    }

    // MARK: - Sessão

    func saveServer(_ raw: String) throws -> URL {
        guard let url = APIClient.normalize(raw) else { throw APIError.server(status: 0, message: "Endereço inválido") }
        serverURLString = url.absoluteString
        UserDefaults.standard.set(serverURLString, forKey: Self.serverKey)
        api.configure(baseURL: url, token: api.token)
        if isLoggedIn { connectRealtime() }
        return url
    }

    func testServer(_ raw: String) async -> Result<String, Error> {
        guard let url = APIClient.normalize(raw) else { return .failure(APIError.server(status: 0, message: "Endereço inválido")) }
        let probe = APIClient()
        probe.configure(baseURL: url, token: nil)
        do {
            let h = try await probe.send(HealthResponse.self, "/api/v1/health")
            return .success("Servidor MonitorIOS v\(h.version) respondeu.")
        } catch {
            return .failure(error)
        }
    }

    func login(username: String, password: String) async throws {
        _ = try saveServer(serverURLString)
        let device = UIDevice.current.name
        let resp = try await api.send(LoginResponse.self, "/api/v1/auth/login", method: "POST",
                                      body: ["username": username, "password": password, "device": device])
        KeychainStore.set(resp.token, for: Self.tokenAccount)
        api.configure(baseURL: api.baseURL, token: resp.token)
        user = resp.user
        isLoggedIn = true
        await refreshAll()
        connectRealtime()
    }

    func logout() {
        let api = self.api
        Task { _ = try? await api.data("/api/v1/auth/logout", method: "POST") }
        clearSession()
    }

    private func handleUnauthorized() {
        guard isLoggedIn else { return }
        clearSession()
    }

    private func clearSession() {
        KeychainStore.set(nil, for: Self.tokenAccount)
        api.configure(baseURL: api.baseURL, token: nil)
        socket.disconnect()
        pollTask?.cancel()
        isLoggedIn = false
        user = nil
        cameras = []
        status = nil
        connection = .unknown
    }

    // MARK: - Dados

    func refreshAll() async {
        await refreshCameras()
        await refreshStatus()
    }

    func refreshCameras() async {
        do {
            cameras = try await api.send([Camera].self, "/api/v1/cameras")
        } catch {
            if (error as? APIError) == .unauthorized { return }
            connection = .offline((error as? LocalizedError)?.errorDescription ?? "Servidor offline")
        }
    }

    func refreshStatus() async {
        do {
            status = try await api.send(SystemStatus.self, "/api/v1/system/status")
            if connection != .online { connection = .online }
        } catch {
            if (error as? APIError) == .unauthorized { return }
            connection = .offline((error as? LocalizedError)?.errorDescription ?? "Servidor offline")
        }
    }

    func state(for cameraId: Int) -> CameraState? { status?.cameras.first { $0.id == cameraId } }

    private func connectRealtime() {
        guard let base = api.baseURL, let token = api.token else { return }
        connection = .connecting
        socket.connect(baseURL: base, token: token)
        // Fallback: se o WebSocket estiver bloqueado, consulta o estado periodicamente.
        pollTask?.cancel()
        pollTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
                guard let self, self.isLoggedIn else { return }
                if self.connection != .online { await self.refreshStatus() }
            }
        }
    }

    func scenePhaseChanged(_ phase: ScenePhase) {
        guard isLoggedIn else { return }
        switch phase {
        case .active:
            socket.restart()
            Task { await refreshAll() }
        case .background:
            socket.disconnect()
        default:
            break
        }
    }
}
