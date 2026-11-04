import AVFoundation
import Foundation

/// Gravações de várias câmeras ao mesmo tempo, no mesmo horário.
///
/// Cada câmera tem o seu ``PlaybackModel`` (playlist do dia, seek por data). Este modelo
/// guarda o horário comum e, a cada ``tick()``, alinha as câmeras a ele:
///  - o horário segue a câmera que está tocando mais atrás;
///  - uma câmera que pulou uma lacuna própria (ou atrasou) volta para o horário comum;
///  - uma câmera parada por falta de gravação volta a tocar quando o horário chega numa gravação dela.
@MainActor
final class MultiPlaybackModel: ObservableObject {
    let models: [PlaybackModel]

    @Published private(set) var day: Date
    @Published private(set) var spans: [TimeSpan] = []
    @Published var cursor: Double
    @Published var span: Double = 3 * 3600
    @Published var isScrubbing = false
    @Published private(set) var wantsPlay = false
    @Published private(set) var rate: Float = 1
    @Published private(set) var notice: String?

    private var lastSync: [Int: Double] = [:]
    private var loadToken = 0

    var dayStart: Double { day.timeIntervalSince1970 }
    var dayEnd: Double { (Calendar.current.date(byAdding: .day, value: 1, to: day) ?? day).timeIntervalSince1970 }
    var isToday: Bool { Calendar.current.isDateInToday(day) }
    var canGoForward: Bool { !isToday && day < Date() }

    init(cameraIds: [Int], api: APIClient) {
        models = cameraIds.map { PlaybackModel(cameraId: $0, api: api) }
        day = Calendar.current.startOfDay(for: Date())
        cursor = Date().timeIntervalSince1970
        for m in models {
            m.followsNextDay = false
            m.player.isMuted = true  // quatro áudios juntos viram ruído
        }
    }

    func open(day newDay: Date, at time: Double? = nil) async {
        loadToken += 1
        let token = loadToken
        wantsPlay = false
        day = Calendar.current.startOfDay(for: newDay)
        notice = nil
        let loads = models.map { m in Task { await m.open(day: newDay, at: time, autoplay: false) } }
        for t in loads { await t.value }
        guard token == loadToken else { return }
        spans = TimelineMath.union(models.map { $0.spans })
        guard let first = spans.first, let last = spans.last else {
            cursor = time ?? dayStart + 12 * 3600
            notice = "Nenhuma gravação em \(Fmt.day.string(from: day))."
            return
        }
        wantsPlay = true
        seek(to: time ?? (isToday ? max(last.end - 60, last.start) : first.start), snapForward: true)
    }

    func seek(to t: Double, snapForward: Bool = false) {
        var t = TimelineMath.clamp(t, dayStart, dayEnd - 1)
        notice = nil
        if case .gap(let next, _) = TimelineMath.locate(t, in: spans) {
            if snapForward, let next {
                t = next
            } else {
                cursor = t
                wantsPlay = false
                models.forEach { $0.pause() }
                var text = "Nenhuma câmera gravou às \(Fmt.epoch(t, Fmt.hourMinute))."
                if let next { text += " Próxima gravação às \(Fmt.epoch(next, Fmt.time))." }
                notice = text
                return
            }
        }
        cursor = t
        let now = Date().timeIntervalSince1970
        for (i, m) in models.enumerated() {
            lastSync[i] = now
            if wantsPlay {
                m.play(at: t)
            } else {
                m.pause()
                m.seek(to: t)
            }
        }
    }

    /// Chamado a cada 0,25 s pela tela enquanto ela está aberta.
    func tick() {
        let merged = TimelineMath.union(models.map { $0.spans })
        if merged != spans { spans = merged }
        guard wantsPlay, !isScrubbing else { return }

        let playing = models.filter { $0.player.timeControlStatus == .playing }
        if let lead = playing.map({ $0.cursor }).min() {
            if lead >= dayStart - 60 && lead <= dayEnd + 60 { cursor = lead }
        } else if !models.contains(where: { $0.isLoading }) {
            // Nenhuma câmera tocando: lacuna em todas ou fim das gravações.
            if case .gap(let next, _) = TimelineMath.locate(cursor, in: spans) {
                if let next {
                    seek(to: next)
                } else if canGoForward, let nextDay = Calendar.current.date(byAdding: .day, value: 1, to: day) {
                    wantsPlay = false
                    Task { await open(day: nextDay) }
                } else {
                    wantsPlay = false
                    notice = "Fim das gravações disponíveis. Use AO VIVO para a imagem atual."
                }
                return
            }
        }

        let now = Date().timeIntervalSince1970
        let tolerance = max(3, Double(rate) * 1.5)
        for (i, m) in models.enumerated() where !m.isLoading && now - (lastSync[i] ?? 0) > 2 {
            let needsSync: Bool
            if m.player.timeControlStatus == .playing {
                needsSync = abs(m.cursor - cursor) > tolerance
            } else if m.player.timeControlStatus == .paused, case .recorded = TimelineMath.locate(cursor, in: m.spans) {
                // Só câmeras paradas: uma que ainda está carregando não deve ser reiniciada.
                needsSync = true
            } else {
                needsSync = false
            }
            if needsSync {
                lastSync[i] = now
                m.play(at: cursor)
            }
        }
    }

    func togglePlay() {
        if wantsPlay {
            pause()
        } else {
            wantsPlay = true
            seek(to: cursor, snapForward: true)
        }
    }

    func pause() {
        wantsPlay = false
        models.forEach { $0.pause() }
    }

    func skip(_ seconds: Double) {
        seek(to: cursor + seconds, snapForward: seconds > 0)
    }

    func setRate(_ r: Float) {
        rate = r
        models.forEach { $0.setRate(r) }
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
        seek(to: t)
    }
}
