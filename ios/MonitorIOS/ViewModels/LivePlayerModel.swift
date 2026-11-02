import AVFoundation
import Combine
import CoreImage
import UIKit

/// Reprodução ao vivo (HLS gerado pelo servidor) com reconexão automática.
@MainActor
final class LivePlayerModel: ObservableObject {
    enum State: Equatable { case idle, loading, playing, failed(String) }

    @Published private(set) var state: State = .idle
    @Published var quality: String
    @Published private(set) var now = Date()

    let player = AVPlayer()
    private var output: AVPlayerItemVideoOutput?
    private var cancellables = Set<AnyCancellable>()
    private var retryTask: Task<Void, Never>?
    private var clock: Timer?
    private var cameraId: Int?
    private weak var api: APIClient?
    private var attempts = 0

    init(quality: String = "sub") {
        self.quality = quality
        player.automaticallyWaitsToMinimizeStalling = true
        player.isMuted = true
    }

    func start(cameraId: Int, api: APIClient) {
        self.cameraId = cameraId
        self.api = api
        attempts = 0
        clock?.invalidate()
        clock = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.now = self?.player.currentItem?.currentDate() ?? Date() }
        }
        Task { await load() }
    }

    func setQuality(_ q: String) {
        guard q != quality else { return }
        quality = q
        attempts = 0
        Task { await load() }
    }

    func stop() {
        retryTask?.cancel()
        clock?.invalidate()
        clock = nil
        cancellables.removeAll()
        player.pause()
        player.replaceCurrentItem(with: nil)
        state = .idle
    }

    private func load() async {
        guard let cameraId, let api else { return }
        retryTask?.cancel()
        state = .loading
        do {
            let info = try await api.send(LiveInfo.self, "/api/v1/cameras/\(cameraId)/live",
                                          query: [URLQueryItem(name: "quality", value: quality)])
            guard let url = api.absoluteURL(info.playlistUrl) else { throw APIError.notConfigured }
            let item = AVPlayerItem(url: url)
            item.preferredForwardBufferDuration = 2
            let out = AVPlayerItemVideoOutput(pixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
            item.add(out)
            output = out
            observe(item)
            player.replaceCurrentItem(with: item)
            player.play()
        } catch {
            fail((error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
        }
    }

    private func observe(_ item: AVPlayerItem) {
        cancellables.removeAll()
        item.publisher(for: \.status)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] status in
                guard let self else { return }
                switch status {
                case .readyToPlay:
                    self.state = .playing
                    self.attempts = 0
                case .failed:
                    self.fail(item.error?.localizedDescription ?? "falha no fluxo")
                default: break
                }
            }
            .store(in: &cancellables)
        NotificationCenter.default.publisher(for: AVPlayerItem.failedToPlayToEndTimeNotification, object: item)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.fail("fluxo interrompido") }
            .store(in: &cancellables)
        NotificationCenter.default.publisher(for: AVPlayerItem.playbackStalledNotification, object: item)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.player.play() }
            .store(in: &cancellables)
    }

    /// Tenta novamente com intervalo crescente (rede móvel, câmera reiniciando etc.).
    private func fail(_ message: String) {
        state = .failed(message)
        attempts += 1
        let delay = min(Double(attempts) * 2, 15)
        retryTask?.cancel()
        retryTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await self?.load()
        }
    }

    func retryNow() {
        attempts = 0
        Task { await load() }
    }

    /// Captura o quadro exibido no momento.
    func captureFrame() -> UIImage? {
        FrameGrabber.capture(output: output, item: player.currentItem)
    }
}

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
