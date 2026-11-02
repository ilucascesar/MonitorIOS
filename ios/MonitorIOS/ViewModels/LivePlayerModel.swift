import AVFoundation
import Combine
import CoreImage
import UIKit

/// Ao vivo de baixa latência.
///
/// O servidor mantém a conexão com cada câmera sempre aberta e repassa os quadros H.264 por
/// WebSocket, começando pelo último keyframe. O app decodifica no hardware (VideoToolbox) e
/// exibe cada quadro no horário certo (JitterClock): a imagem aparece em menos de 1 s e fica
/// fluida com atraso mínimo. Se a rede cair, reconecta sozinho mantendo a última imagem na tela.
@MainActor
final class LivePlayerModel: ObservableObject {
    enum State: Equatable { case idle, loading, playing, failed(String) }

    @Published private(set) var state: State = .idle
    @Published var quality: String
    @Published private(set) var now = Date()
    @Published private(set) var hasImage = false
    @Published private(set) var isMuted = true
    @Published private(set) var hasAudio = false
    @Published private(set) var videoSize: CGSize = .zero

    let renderView = VideoRenderView()

    private static let session: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.timeoutIntervalForRequest = 15
        cfg.waitsForConnectivity = false
        return URLSession(configuration: cfg)
    }()

    private var task: URLSessionWebSocketTask?
    private var receiver: Task<Void, Never>?
    private var retryTask: Task<Void, Never>?
    private var cameraId: Int?
    private weak var api: APIClient?
    private var attempts = 0
    private var generation = 0
    private var connectedAt = Date.distantPast
    private var lastFrameAt = Date.distantPast
    private var lastPing = Date.distantPast
    private let decoder = VideoDecoder()
    private let audio = MulawAudioPlayer()
    private var clock = JitterClock()
    private var pending: [(image: CVPixelBuffer, target: Double)] = []
    private var displayLink: DisplayLinkProxy?
    private var timer: Timer?
    private var observers: [NSObjectProtocol] = []
    private var lastImage: CVPixelBuffer?

    init(quality: String = "main") {
        self.quality = quality
        // Chamado no thread do VideoToolbox: não herda o MainActor.
        decoder.onFrame = { @Sendable [weak self] image, target in
            DispatchQueue.main.async {
                MainActor.assumeIsolated { self?.decoded(image, target: target) }
            }
        }
    }

    // MARK: - Controle

    func start(cameraId: Int, api: APIClient) {
        self.cameraId = cameraId
        self.api = api
        attempts = 0
        if displayLink == nil {
            displayLink = DisplayLinkProxy { [weak self] in
                MainActor.assumeIsolated { self?.tick() }
            }
        }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.watchdog() }
        }
        if observers.isEmpty {
            let nc = NotificationCenter.default
            observers.append(nc.addObserver(forName: UIApplication.didEnterBackgroundNotification, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.suspend() }
            })
            observers.append(nc.addObserver(forName: UIApplication.willEnterForegroundNotification, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.resume() }
            })
        }
        connect()
    }

    func stop() {
        retryTask?.cancel()
        retryTask = nil
        disconnect()
        displayLink?.invalidate()
        displayLink = nil
        timer?.invalidate()
        timer = nil
        observers.forEach { NotificationCenter.default.removeObserver($0) }
        observers.removeAll()
        audio.stop()
        decoder.invalidate()
        pending.removeAll()
        state = .idle
    }

    func setQuality(_ q: String) {
        guard q != quality else { return }
        quality = q
        attempts = 0
        connect()
    }

    func retryNow() {
        attempts = 0
        connect()
    }

    func toggleMute() {
        isMuted.toggle()
        if isMuted { audio.stop() } else { audio.start() }
    }

    /// Quadro exibido no momento, em resolução original.
    func captureFrame() -> UIImage? {
        guard let lastImage else { return nil }
        let ci = CIImage(cvPixelBuffer: lastImage)
        guard let cg = CIContext().createCGImage(ci, from: ci.extent) else { return nil }
        return UIImage(cgImage: cg)
    }

    // MARK: - Conexão

    private func connect() {
        retryTask?.cancel()
        disconnect()
        guard let cameraId, let api,
              let url = api.webSocketURL("/api/v1/live/\(cameraId)/ws", query: [URLQueryItem(name: "quality", value: quality)])
        else {
            state = .failed(APIError.notConfigured.errorDescription ?? "Servidor não configurado")
            return
        }
        var req = URLRequest(url: url)
        req.timeoutInterval = 15
        if let token = api.token { req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        let task = Self.session.webSocketTask(with: req)
        task.maximumMessageSize = 16 * 1024 * 1024
        generation += 1
        let gen = generation
        self.task = task
        if state != .playing { state = .loading }
        connectedAt = Date()
        lastFrameAt = .distantPast
        clock.reset()
        task.resume()
        receiver = Task { [weak self] in await self?.receiveLoop(task, generation: gen) }
    }

    private func disconnect() {
        receiver?.cancel()
        receiver = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    private func receiveLoop(_ task: URLSessionWebSocketTask, generation gen: Int) async {
        while !Task.isCancelled {
            let message: URLSessionWebSocketTask.Message
            do {
                message = try await task.receive()
            } catch {
                guard gen == generation, !Task.isCancelled else { return }
                fail(lastFrameAt == .distantPast ? "Não foi possível abrir o ao vivo." : "Conexão interrompida.")
                return
            }
            guard gen == generation else { return }
            switch message {
            case .data(let data): handle(data)
            case .string(let text): handleText(text)
            @unknown default: break
            }
        }
    }

    private func handle(_ data: Data) {
        guard let msg = LiveWire.parse(data) else { return }
        switch msg.kind {
        case LiveWire.videoConfig:
            guard let codec = msg.payload.first else { return }
            guard decoder.configure(codec: codec, record: [UInt8](msg.payload.dropFirst())) else {
                fail("Formato de vídeo não suportado pelo iPhone.")
                return
            }
            let size = decoder.dimensions
            if size != videoSize { videoSize = size }
        case LiveWire.video:
            let arrival = CACurrentMediaTime()
            lastFrameAt = Date()
            // Quadros do GOP guardado só preparam o decodificador; o último já é o presente.
            let target = msg.isCatchUp ? arrival : clock.target(ptsSeconds: Double(msg.pts) / 1000, arrival: arrival)
            decoder.decode(msg.payload, pts: msg.pts, isKey: msg.isKey, display: !msg.isCatchUp, target: target)
        case LiveWire.audioConfig:
            hasAudio = true
        case LiveWire.audio:
            if !isMuted { audio.play(msg.payload) }
        default:
            break
        }
    }

    private func handleText(_ text: String) {
        guard let obj = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any],
              let type = obj["type"] as? String else { return }
        if type == "reset" {
            // A câmera caiu e o servidor está reconectando: novos horários a partir do próximo keyframe.
            clock.reset()
            pending.removeAll()
        }
    }

    // MARK: - Exibição

    private func decoded(_ image: CVPixelBuffer, target: Double) {
        guard displayLink != nil else { return }
        let now = CACurrentMediaTime()
        if target < now - 0.04 { clock.frameWasLate(now: now) }
        pending.append((image, target))
        if pending.count > 30 { pending.removeFirst(pending.count - 2) }
    }

    private func tick() {
        let now = CACurrentMediaTime()
        var show: CVPixelBuffer?
        while let first = pending.first, first.target <= now {
            show = first.image
            pending.removeFirst()
        }
        guard let show else { return }
        renderView.show(show)
        lastImage = show
        if !hasImage { hasImage = true }
        if state != .playing {
            state = .playing
            attempts = 0
        }
        clock.relax(now: now)
    }

    private func watchdog() {
        now = Date()
        guard task != nil else { return }
        let reference = lastFrameAt == .distantPast ? connectedAt : lastFrameAt
        let silent = Date().timeIntervalSince(reference)
        if silent > (lastFrameAt == .distantPast ? 15 : 6) {
            fail(lastFrameAt == .distantPast ? "A câmera não respondeu." : "Sem imagem da câmera.")
            return
        }
        if Date().timeIntervalSince(lastPing) > 15 {
            lastPing = Date()
            task?.sendPing { _ in }
        }
    }

    /// Reconecta com intervalo crescente, mantendo a última imagem na tela.
    private func fail(_ message: String) {
        disconnect()
        state = .failed(message)
        attempts += 1
        let delay = min(Double(attempts), 5)
        retryTask?.cancel()
        retryTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.connect()
        }
    }

    private func suspend() {
        retryTask?.cancel()
        disconnect()
        audio.stop()
        decoder.invalidate()
        pending.removeAll()
    }

    private func resume() {
        guard displayLink != nil else { return }
        if !isMuted { audio.start() }
        attempts = 0
        connect()
    }
}

/// Captura de quadro do AVPlayer (gravações).
enum FrameGrabber {
    static func capture(output: AVPlayerItemVideoOutput?, item: AVPlayerItem?) -> UIImage? {
        guard let output, let item else { return nil }
        let time = item.currentTime()
        guard let buffer = output.copyPixelBuffer(forItemTime: time, itemTimeForDisplay: nil) else { return nil }
        let ci = CIImage(cvPixelBuffer: buffer)
        guard let cg = CIContext().createCGImage(ci, from: ci.extent) else { return nil }
        return UIImage(cgImage: cg)
    }
}
