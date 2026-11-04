import Foundation

/// Cálculos puros da linha do tempo (testados em MonitorIOSTests).
enum TimelineMath {
    /// Escalas de marcação, em segundos, da mais fina à mais grossa.
    static let tickSteps: [Double] = [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600]
    static let minSpan: Double = 5 * 60
    static let maxSpan: Double = 24 * 3600

    static func x(for t: Double, cursor: Double, span: Double, width: Double) -> Double {
        width / 2 + (t - cursor) * (width / span)
    }

    static func time(forX x: Double, cursor: Double, span: Double, width: Double) -> Double {
        cursor + (x - width / 2) * (span / width)
    }

    /// Menor passo de marcação que mantém pelo menos ``minPixels`` entre as marcas.
    static func tickStep(span: Double, width: Double, minPixels: Double = 56) -> Double {
        let pxPerSecond = width / span
        return tickSteps.first { $0 * pxPerSecond >= minPixels } ?? tickSteps.last!
    }

    static func clampSpan(_ span: Double) -> Double { min(max(span, minSpan), maxSpan) }

    static func clamp(_ t: Double, _ lo: Double, _ hi: Double) -> Double { min(max(t, lo), hi) }

    enum Location: Equatable {
        case recorded(TimeSpan)
        case gap(next: Double?, previousEnd: Double?)
    }

    static func locate(_ t: Double, in spans: [TimeSpan]) -> Location {
        var prevEnd: Double?
        for s in spans {
            if s.contains(t) { return .recorded(s) }
            if s.start > t { return .gap(next: s.start, previousEnd: prevEnd) }
            prevEnd = s.end
        }
        return .gap(next: nil, previousEnd: prevEnd)
    }

    /// Posição na playlist HLS (que omite as lacunas) correspondente ao horário real ``t``.
    static func playlistOffset(for t: Double, in spans: [TimeSpan]) -> Double? {
        var acc = 0.0
        for s in spans {
            if s.contains(t) { return acc + (t - s.start) }
            if s.start > t { return nil }
            acc += s.duration
        }
        return nil
    }

    static func recordedSeconds(_ spans: [TimeSpan]) -> Double { spans.reduce(0) { $0 + $1.duration } }

    /// Junta as gravações de várias câmeras numa lista ordenada e sem sobreposição.
    static func union(_ lists: [[TimeSpan]]) -> [TimeSpan] {
        var out: [TimeSpan] = []
        for s in lists.flatMap({ $0 }).sorted(by: { $0.start < $1.start }) {
            if let last = out.last, s.start <= last.end {
                out[out.count - 1] = TimeSpan(start: last.start, end: max(last.end, s.end))
            } else {
                out.append(s)
            }
        }
        return out
    }
}
