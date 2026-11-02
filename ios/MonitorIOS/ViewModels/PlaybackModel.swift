import AVFoundation
import Combine
import UIKit

/// Reprodução de gravações de um dia inteiro.
///
/// O servidor entrega UMA playlist HLS VOD por dia, com EXT-X-PROGRAM-DATE-TIME em cada parte.
/// Isso permite:
///  - `AVPlayerItem.seek(to: Date)` para ir ao horário escolhido na linha do tempo, sem recarregar;
///  - `currentDate()` para mostrar o horário real da imagem;
///  - passagem automática entre segmentos e sobre lacunas (EXT-X-DISCONTINUITY).
@MainActor
final class PlaybackModel: ObservableObject {
    struct Notice: Equatable {
        let text: String
        let next: Double?
    }

    @Published private(set) var day: Date
    @Published private(set) var spans: [TimeSpan] = []
    @Published var cursor: Double
    @Published var span: Double = 3 * 3600
    @Published var isScrubbing = false
    @Published private(set) var isPlaying = false
    @Published private(set) var isLoading = false
    @Published private(set) var notice: Notice?
    @Published private(set) var error: String?
    @Published private(set) var recordedDays: [String: Double] = [:]
    @Published private(set) var rate: Float = 1

    let player = AVPlayer()
    let cameraId: Int
    private let api: APIClient
    private var output: AVPlayerItemVideoOutput?
    private var cancellables = Set<AnyCancellable>()
    private var itemCancellables = Set<AnyCancellable>()
    private var timeObserver: Any?
    private var pendingSeek: Double?
    private var wantsPlay = true
    private var loadToken = 0

    var dayStart: Double { day.timeIntervalSince1970 }
    var dayEnd: Double { (Calendar.current.date(byAdding: .day, value: 1, to: day) ?? day).timeIntervalSince1970 }
    var isToday: Bool { Calendar.current.isDateInToday(day) }
    var canGoForward: Bool { !isToday && day < Date() }

    init(cameraId: Int, api: APIClient, day: Date = Date()) {
        self.cameraId = cameraId
        self.api = api
        self.day = Calendar.current.startOfDay(for: day)
        self.cursor = Date().timeIntervalSince1970
        // Começa a tocar assim que o primeiro trecho chega (o servidor lê do disco local do PC).
        player.automaticallyWaitsToMinimizeStalling = false
        timeObserver = player.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.25, preferredTimescale: 600), queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
        player.publisher(for: \.timeControlStatus)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] s in self?.isPlaying = (s != .paused) }
            .store(in: &cancellables)
    }

    deinit {
        if let timeObserver { player.removeTimeObserver(timeObserver) }
    }

    // MARK: - Carregamento

    func open(day newDay: Date, at time: Double? = nil, autoplay: Bool = true) async {
        loadToken += 1
        let token = loadToken
        day = Calendar.current.startOfDay(for: newDay)
        isLoading = true
        error = nil
        notice = nil
        wantsPlay = autoplay
        let end = min(dayEnd, Date().timeIntervalSince1970 + 5)
        do {
            let info = try await api.send(PlaybackInfo.self, "/api/v1/cameras/\(cameraId)/playback", query: [
                URLQueryItem(name: "start", value: String(dayStart)),
                URLQueryItem(name: "end", value: String(end)),
            ])
            guard token == loadToken else { return }
            spans = info.intervals.compactMap { $0.count == 2 ? TimeSpan(start: $0[0], end: $0[1]) : nil }
            guard let first = spans.first, let last = spans.last, let url = api.absoluteURL(info.playlistUrl) else {
                player.replaceCurrentItem(with: nil)
                cursor = time ?? dayStart + 12 * 3600
                notice = Notice(text: "Nenhuma gravação em \(Fmt.day.string(from: day)).", next: nil)
                isLoading = false
                return
            }
            let item = AVPlayerItem(url: url)
            item.preferredForwardBufferDuration = 10
            let out = AVPlayerItemVideoOutput(pixelBufferAttributes: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
            item.add(out)
            output = out
            observe(item)
            player.replaceCurrentItem(with: item)
            let target = time ?? (isToday ? max(last.end - 60, last.start) : first.start)
            seek(to: target, snapForward: true)
        } catch {
            guard token == loadToken else { return }
            self.error = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            isLoading = false
        }
    }

    func loadMonth(_ month: Date) async {
        let key = Fmt.isoMonth.string(from: month)
        do {
            let resp = try await api.send(DaysResponse.self, "/api/v1/cameras/\(cameraId)/days", query: [
                URLQueryItem(name: "month", value: key),
                URLQueryItem(name: "tz", value: TimeZone.current.identifier),
            ])
            var merged = recordedDays.filter { !$0.key.hasPrefix(key) }
            for d in resp.days { merged[d.date] = d.recordedSeconds }
            recordedDays = merged
        } catch {
            // Calendário sem destaque; não bloqueia a reprodução.
        }
    }

    private func observe(_ item: AVPlayerItem) {
        itemCancellables.removeAll()
        item.publisher(for: \.status)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] status in
                guard let self else { return }
                switch status {
                case .readyToPlay:
                    self.isLoading = false
                    if let t = self.pendingSeek {
                        self.pendingSeek = nil
                        self.performSeek(t)
                    }
                case .failed:
                    self.isLoading = false
                    self.error = item.error?.localizedDescription ?? "Falha ao reproduzir"
                default: break
                }
            }
            .store(in: &itemCancellables)
        NotificationCenter.default.publisher(for: AVPlayerItem.didPlayToEndTimeNotification, object: item)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in Task { await self?.reachedEnd() } }
            .store(in: &itemCancellables)
        NotificationCenter.default.publisher(for: AVPlayerItem.failedToPlayToEndTimeNotification, object: item)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                // Rede oscilou: recarrega a playlist do mesmo ponto.
                guard let self else { return }
                Task { await self.open(day: self.day, at: self.cursor, autoplay: true) }
            }
            .store(in: &itemCancellables)
    }

    /// Fim da playlist: hoje -> busca novos segmentos; dia anterior -> continua no dia seguinte.
    private func reachedEnd() async {
        if isToday {
            let before = spans.last?.end ?? 0
            await open(day: day, at: cursor, autoplay: true)
            if let after = spans.last?.end, after - before < 5 {
                player.pause()
                notice = Notice(text: "Fim das gravações disponíveis. Use AO VIVO para a imagem atual.", next: nil)
            }
        } else if let next = Calendar.current.date(byAdding: .day, value: 1, to: day) {
            await open(day: next, at: nil, autoplay: true)
        }
    }

    // MARK: - Controles

    private func tick() {
        guard !isScrubbing, pendingSeek == nil, let d = player.currentItem?.currentDate() else { return }
        let t = d.timeIntervalSince1970
        if t >= dayStart - 60 && t <= dayEnd + 60 { cursor = t }
    }

    /// Vai para o horário ``t``. Em lacunas: avança para a próxima gravação (``snapForward``)
    /// ou pausa e informa o próximo horário disponível.
    func seek(to t: Double, snapForward: Bool = false) {
        let t = TimelineMath.clamp(t, dayStart, dayEnd - 1)
        switch TimelineMath.locate(t, in: spans) {
        case .recorded:
            notice = nil
            performSeek(t)
        case .gap(let next, _):
            if snapForward, let next {
                notice = nil
                performSeek(next)
            } else {
                cursor = t
                player.pause()
                var text = "Sem gravação às \(Fmt.epoch(t, Fmt.hourMinute))."
                if let next { text += " Próxima gravação às \(Fmt.epoch(next, Fmt.time))." }
                else if canGoForward { text += " Não há mais gravações neste dia." }
                notice = Notice(text: text, next: next)
                isLoading = false
            }
        }
    }

    private func performSeek(_ t: Double) {
        cursor = t
        guard let item = player.currentItem else { return }
        guard item.status == .readyToPlay else {
            pendingSeek = t
            return
        }
        let spans = self.spans
        let date = Date(timeIntervalSince1970: t)
        let ok = item.seek(to: date) { [weak self] finished in
            Task { @MainActor in
                guard let self else { return }
                if !finished, let off = TimelineMath.playlistOffset(for: t, in: spans) {
                    self.fallbackSeek(off)
                } else if self.wantsPlay {
                    self.player.playImmediately(atRate: self.rate)
                }
            }
        }
        if !ok, let off = TimelineMath.playlistOffset(for: t, in: spans) {
            fallbackSeek(off)
        }
    }

    private func fallbackSeek(_ offset: Double) {
        player.seek(to: CMTime(seconds: offset, preferredTimescale: 600), toleranceBefore: .zero,
                    toleranceAfter: CMTime(seconds: 1, preferredTimescale: 600)) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                if self.wantsPlay { self.player.playImmediately(atRate: self.rate) }
            }
        }
    }

    func togglePlay() {
        if isPlaying {
            wantsPlay = false
            player.pause()
        } else {
            wantsPlay = true
            if notice?.next != nil, let next = notice?.next {
                seek(to: next)
            } else {
                player.playImmediately(atRate: rate)
            }
        }
    }

    func skip(_ seconds: Double) {
        seek(to: cursor + seconds, snapForward: seconds > 0)
    }

    func setRate(_ r: Float) {
        rate = r
        if isPlaying { player.rate = r }
    }

    func goToNextAvailable() {
        guard let next = notice?.next else { return }
        wantsPlay = true
        seek(to: next)
    }

    func shiftDay(_ delta: Int) async {
        guard let d = Calendar.current.date(byAdding: .day, value: delta, to: day), d <= Date() else { return }
        await open(day: d)
    }

    func beginScrub() {
        isScrubbing = true
    }

    func endScrub(at t: Double) {
        isScrubbing = false
        seek(to: t, snapForward: false)
    }

    func pause() {
        wantsPlay = false
        player.pause()
    }

    func captureFrame() -> UIImage? {
        FrameGrabber.capture(output: output, item: player.currentItem)
    }
}
