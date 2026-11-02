import SwiftUI

/// Linha do tempo de 24 h: o cursor fica fixo no centro e a régua desliza sob ele.
/// - arrastar: escolhe o horário (a reprodução salta ao soltar);
/// - pinça: aproxima/afasta (5 min a 24 h);
/// - toque: vai direto ao horário tocado.
struct TimelineBar: View {
    let dayStart: Double
    let dayEnd: Double
    let spans: [TimeSpan]
    @Binding var cursor: Double
    @Binding var span: Double
    var onScrubBegan: () -> Void = {}
    var onScrubEnded: (Double) -> Void = { _ in }

    @State private var dragOrigin: Double?
    @State private var zoomOrigin: Double?

    var body: some View {
        GeometryReader { geo in
            let w = Double(max(geo.size.width, 1))
            Canvas { ctx, size in
                draw(&ctx, size: size)
            }
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 3)
                    .onChanged { v in
                        if dragOrigin == nil {
                            dragOrigin = cursor
                            onScrubBegan()
                        }
                        let t = (dragOrigin ?? cursor) - Double(v.translation.width) * span / w
                        cursor = TimelineMath.clamp(t, dayStart, dayEnd - 1)
                    }
                    .onEnded { _ in
                        dragOrigin = nil
                        onScrubEnded(cursor)
                    }
            )
            .simultaneousGesture(
                MagnifyGesture()
                    .onChanged { v in
                        if zoomOrigin == nil { zoomOrigin = span }
                        span = TimelineMath.clampSpan((zoomOrigin ?? span) / Double(v.magnification))
                    }
                    .onEnded { _ in zoomOrigin = nil }
            )
            .simultaneousGesture(
                SpatialTapGesture()
                    .onEnded { v in
                        let t = TimelineMath.time(forX: Double(v.location.x), cursor: cursor, span: span, width: w)
                        onScrubBegan()
                        cursor = TimelineMath.clamp(t, dayStart, dayEnd - 1)
                        onScrubEnded(cursor)
                    }
            )
        }
        .accessibilityElement()
        .accessibilityLabel("Linha do tempo")
        .accessibilityValue(Fmt.epoch(cursor, Fmt.time))
        .accessibilityAdjustableAction { dir in
            onScrubBegan()
            cursor = TimelineMath.clamp(cursor + (dir == .increment ? 300 : -300), dayStart, dayEnd - 1)
            onScrubEnded(cursor)
        }
    }

    private func draw(_ ctx: inout GraphicsContext, size: CGSize) {
        let w = Double(size.width)
        let h = Double(size.height)
        let barY = h * 0.42
        let barH = h * 0.32
        let visStart = cursor - span / 2
        let visEnd = cursor + span / 2
        func x(_ t: Double) -> Double { TimelineMath.x(for: t, cursor: cursor, span: span, width: w) }

        // Trilho (fora do dia fica mais escuro)
        ctx.fill(Path(roundedRect: CGRect(x: 0, y: barY, width: w, height: barH), cornerRadius: 4),
                 with: .color(.white.opacity(0.05)))
        let d0 = max(0, x(dayStart)), d1 = min(w, x(dayEnd))
        if d1 > d0 {
            ctx.fill(Path(CGRect(x: d0, y: barY, width: d1 - d0, height: barH)), with: .color(.white.opacity(0.10)))
        }

        // Períodos gravados
        for s in spans where s.end > visStart && s.start < visEnd {
            let a = max(0, x(s.start)), b = min(w, x(s.end))
            ctx.fill(Path(CGRect(x: a, y: barY, width: max(1.5, b - a), height: barH)), with: .color(Color.accentColor))
        }

        // "Agora" (dia atual)
        let now = Date().timeIntervalSince1970
        if now > visStart && now < visEnd {
            let nx = x(now)
            ctx.fill(Path(CGRect(x: nx, y: barY, width: max(0, w - nx), height: barH)),
                     with: .color(.black.opacity(0.35)))
        }

        // Régua
        let step = TimelineMath.tickStep(span: span, width: w)
        var t = dayStart + floor((visStart - dayStart) / step) * step
        while t <= visEnd + step {
            let px = x(t)
            if px >= -40 && px <= w + 40 && t >= dayStart && t <= dayEnd {
                let major = (t - dayStart).truncatingRemainder(dividingBy: max(step * 2, 3600)) == 0
                ctx.fill(Path(CGRect(x: px - 0.5, y: barY + barH, width: 1, height: major ? 8 : 5)),
                         with: .color(.white.opacity(major ? 0.7 : 0.4)))
                let label = t >= dayEnd ? "24:00" : Fmt.epoch(t, Fmt.hourMinute)
                ctx.draw(Text(label).font(.system(size: 10, weight: major ? .semibold : .regular).monospacedDigit())
                            .foregroundStyle(.white.opacity(major ? 0.85 : 0.55)),
                         at: CGPoint(x: px, y: barY - 9))
            }
            t += step
        }

        // Cursor central
        let cx = w / 2
        ctx.fill(Path(CGRect(x: cx - 1, y: 2, width: 2, height: h - 4)), with: .color(.yellow))
        var tri = Path()
        tri.move(to: CGPoint(x: cx - 6, y: 0))
        tri.addLine(to: CGPoint(x: cx + 6, y: 0))
        tri.addLine(to: CGPoint(x: cx, y: 7))
        tri.closeSubpath()
        ctx.fill(tri, with: .color(.yellow))
    }
}
