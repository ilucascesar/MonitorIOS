import SwiftUI

/// Calendário mensal que destaca os dias com gravação (intensidade = horas gravadas).
struct MonthCalendarView: View {
    @Binding var month: Date
    let selected: Date
    let recorded: [String: Double]
    let onSelect: (Date) -> Void

    private var cal: Calendar {
        var c = Calendar(identifier: .gregorian)
        c.locale = Fmt.locale
        c.timeZone = .current
        c.firstWeekday = 1
        return c
    }

    private var days: [Date?] {
        guard let interval = cal.dateInterval(of: .month, for: month) else { return [] }
        let first = interval.start
        let leading = (cal.component(.weekday, from: first) - cal.firstWeekday + 7) % 7
        let count = cal.range(of: .day, in: .month, for: first)?.count ?? 30
        var out: [Date?] = Array(repeating: nil, count: leading)
        for i in 0..<count { out.append(cal.date(byAdding: .day, value: i, to: first)) }
        return out
    }

    var body: some View {
        VStack(spacing: 12) {
            HStack {
                Button { shift(-1) } label: { Image(systemName: "chevron.left") }
                    .accessibilityLabel("Mês anterior")
                Spacer()
                Text(Fmt.month.string(from: month).capitalized).font(.headline)
                Spacer()
                Button { shift(1) } label: { Image(systemName: "chevron.right") }
                    .disabled(cal.isDate(month, equalTo: Date(), toGranularity: .month))
                    .accessibilityLabel("Próximo mês")
            }
            .buttonStyle(.bordered)

            let symbols = cal.veryShortStandaloneWeekdaySymbols
            HStack {
                ForEach(0..<7, id: \.self) { i in
                    Text(symbols[(i + cal.firstWeekday - 1) % 7].uppercased())
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity)
                }
            }

            LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 6), count: 7), spacing: 6) {
                ForEach(Array(days.enumerated()), id: \.offset) { _, day in
                    if let day { cell(day) } else { Color.clear.frame(height: 40) }
                }
            }

            HStack(spacing: 12) {
                legend(Color.accentColor, "com gravação")
                legend(Color.accentColor.opacity(0.35), "parcial")
                legend(Color.gray.opacity(0.25), "sem gravação")
            }
            .font(.caption2)
            .foregroundStyle(.secondary)
        }
    }

    private func cell(_ day: Date) -> some View {
        let key = Fmt.isoDay.string(from: day)
        let seconds = recorded[key] ?? 0
        let future = day > Date()
        let isSelected = cal.isDate(day, inSameDayAs: selected)
        let fill: Color = seconds <= 0 ? Color.gray.opacity(0.15) : Color.accentColor.opacity(0.3 + 0.7 * min(seconds / 86400, 1))
        return Button {
            onSelect(day)
        } label: {
            VStack(spacing: 2) {
                Text("\(cal.component(.day, from: day))").font(.callout.weight(isSelected ? .bold : .regular))
                if seconds > 0 {
                    Text(Fmt.duration(seconds).replacingOccurrences(of: "min", with: "m")).font(.system(size: 8))
                }
            }
            .frame(maxWidth: .infinity, minHeight: 40)
            .background(fill, in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(isSelected ? Color.yellow : .clear, lineWidth: 2))
            .foregroundStyle(future ? Color.secondary : Color.primary)
        }
        .buttonStyle(.plain)
        .disabled(future)
        .accessibilityLabel("\(Fmt.day.string(from: day)), \(seconds > 0 ? Fmt.duration(seconds) + " gravados" : "sem gravação")")
    }

    private func legend(_ color: Color, _ text: String) -> some View {
        HStack(spacing: 4) {
            RoundedRectangle(cornerRadius: 3).fill(color).frame(width: 12, height: 12)
            Text(text)
        }
    }

    private func shift(_ delta: Int) {
        if let m = cal.date(byAdding: .month, value: delta, to: month) { month = m }
    }
}
