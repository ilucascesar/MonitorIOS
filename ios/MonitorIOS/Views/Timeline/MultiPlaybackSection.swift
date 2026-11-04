import SwiftUI

/// Gravações de até 4 câmeras numa grade, no mesmo horário, com uma linha do tempo comum.
/// Toque duplo numa câmera abre só ela.
struct MultiPlaybackSection: View {
    let cameras: [Camera]
    let onOpenCamera: (Camera) -> Void
    @StateObject private var vm: MultiPlaybackModel
    @State private var showCalendar = false

    init(cameras: [Camera], api: APIClient, onOpenCamera: @escaping (Camera) -> Void) {
        self.cameras = cameras
        self.onOpenCamera = onOpenCamera
        _vm = StateObject(wrappedValue: MultiPlaybackModel(cameraIds: cameras.map { $0.id }, api: api))
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 10) {
                dayHeader
                grid
                if let notice = vm.notice {
                    NoticeBanner(text: notice)
                }
                timeline
                controls
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 16)
        }
        .task {
            if vm.spans.isEmpty && vm.notice == nil {
                await vm.open(day: Date())
                await vm.models.first?.loadMonth(Date())
            }
        }
        .task {
            while !Task.isCancelled {
                vm.tick()
                try? await Task.sleep(nanoseconds: 250_000_000)
            }
        }
        .onDisappear { vm.pause() }
        .sheet(isPresented: $showCalendar) {
            if let first = vm.models.first {
                CalendarSheet(vm: first) { day in
                    showCalendar = false
                    Task { await vm.open(day: day) }
                }
                .presentationDetents([.medium, .large])
            }
        }
    }

    private var dayHeader: some View {
        HStack {
            Button { Task { await vm.shiftDay(-1) } } label: { Image(systemName: "chevron.left") }
                .accessibilityLabel("Dia anterior")
            Spacer()
            Button { showCalendar = true } label: {
                Label(Fmt.dayLong.string(from: vm.day).capitalized, systemImage: "calendar")
                    .font(.subheadline.weight(.semibold))
            }
            Spacer()
            Button { Task { await vm.shiftDay(1) } } label: { Image(systemName: "chevron.right") }
                .disabled(!vm.canGoForward)
                .accessibilityLabel("Próximo dia")
        }
        .buttonStyle(.bordered)
    }

    private var grid: some View {
        LazyVGrid(columns: [GridItem(.flexible(), spacing: 4), GridItem(.flexible(), spacing: 4)], spacing: 4) {
            ForEach(cameras.indices, id: \.self) { i in
                PlaybackTile(name: cameras[i].name, model: vm.models[i], hasRecording: hasRecording(vm.models[i]))
                    .onTapGesture(count: 2) { onOpenCamera(cameras[i]) }
            }
        }
    }

    private func hasRecording(_ m: PlaybackModel) -> Bool {
        if case .recorded = TimelineMath.locate(vm.cursor, in: m.spans) { return true }
        return false
    }

    private var timeline: some View {
        VStack(spacing: 4) {
            HStack {
                Text(Fmt.epoch(vm.cursor, Fmt.time))
                    .font(.title2.monospacedDigit().weight(.semibold))
                Spacer()
                Button { vm.span = TimelineMath.clampSpan(vm.span * 2) } label: { Image(systemName: "minus.magnifyingglass") }
                    .accessibilityLabel("Afastar")
                Button { vm.span = TimelineMath.clampSpan(vm.span / 2) } label: { Image(systemName: "plus.magnifyingglass") }
                    .accessibilityLabel("Aproximar")
            }
            TimelineBar(dayStart: vm.dayStart, dayEnd: vm.dayEnd, spans: vm.spans, cursor: $vm.cursor, span: $vm.span,
                        onScrubBegan: { vm.beginScrub() }, onScrubEnded: { vm.endScrub(at: $0) })
                .frame(height: 64)
                .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 10))
        }
    }

    private var controls: some View {
        HStack(spacing: 18) {
            Button { vm.skip(-60) } label: { Image(systemName: "gobackward.60") }.accessibilityLabel("Voltar 1 minuto")
            Button { vm.skip(-10) } label: { Image(systemName: "gobackward.10") }.accessibilityLabel("Voltar 10 segundos")
            Button { vm.togglePlay() } label: {
                Image(systemName: vm.wantsPlay ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 46))
            }
            .accessibilityLabel(vm.wantsPlay ? "Pausar" : "Reproduzir")
            Button { vm.skip(10) } label: { Image(systemName: "goforward.10") }.accessibilityLabel("Avançar 10 segundos")
            Menu {
                ForEach([Float(1), 2, 4, 8, 16], id: \.self) { r in
                    Button("\(Int(r))×") { vm.setRate(r) }
                }
            } label: {
                Text("\(Int(vm.rate))×").font(.headline.monospacedDigit())
            }
            .accessibilityLabel("Velocidade")
        }
        .font(.title2)
        .padding(.vertical, 4)
    }
}

private struct PlaybackTile: View {
    let name: String
    @ObservedObject var model: PlaybackModel
    let hasRecording: Bool

    var body: some View {
        ZStack(alignment: .topLeading) {
            PlayerLayerView(player: model.player)
            if model.isLoading {
                ProgressView().tint(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if !hasRecording {
                Text("Sem gravação")
                    .font(.caption2)
                    .foregroundStyle(.white.opacity(0.7))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            Text(name)
                .font(.caption2.weight(.semibold))
                .padding(.horizontal, 5).padding(.vertical, 2)
                .background(.black.opacity(0.55), in: Capsule())
                .foregroundStyle(.white)
                .padding(4)
        }
        .aspectRatio(16 / 9, contentMode: .fit)
        .background(Color.black)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .contentShape(Rectangle())
    }
}
