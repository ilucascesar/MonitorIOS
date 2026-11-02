import Foundation

enum Fmt {
    static let locale = Locale(identifier: "pt_BR")

    private static func make(_ format: String) -> DateFormatter {
        let f = DateFormatter()
        f.locale = locale
        f.timeZone = .current
        f.dateFormat = format
        return f
    }

    static let time = make("HH:mm:ss")
    static let hourMinute = make("HH:mm")
    static let dateTime = make("dd/MM/yyyy HH:mm:ss")
    static let day = make("dd/MM/yyyy")
    static let dayLong = make("EEEE, d 'de' MMMM")
    static let month = make("LLLL 'de' yyyy")
    static let isoDay: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM-dd"
        return f
    }()
    static let isoMonth: DateFormatter = {
        let f = DateFormatter()
        f.locale = Locale(identifier: "en_US_POSIX")
        f.timeZone = .current
        f.dateFormat = "yyyy-MM"
        return f
    }()

    static func epoch(_ t: Double, _ f: DateFormatter = time) -> String {
        f.string(from: Date(timeIntervalSince1970: t))
    }

    static func bytes(_ b: Double?) -> String {
        guard let b else { return "—" }
        return ByteCountFormatter.string(fromByteCount: Int64(b), countStyle: .file)
    }

    static func relative(_ t: Double?) -> String {
        guard let t else { return "nunca" }
        let f = RelativeDateTimeFormatter()
        f.locale = locale
        f.unitsStyle = .short
        return f.localizedString(for: Date(timeIntervalSince1970: t), relativeTo: Date())
    }

    static func duration(_ s: Double) -> String {
        let h = Int(s) / 3600, m = Int(s) % 3600 / 60
        return h > 0 ? "\(h)h \(m)min" : "\(m)min"
    }
}
