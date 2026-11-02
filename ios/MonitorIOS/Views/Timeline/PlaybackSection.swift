import AVFoundation
import SwiftUI

/// Tela de gravações de uma câmera: dia, player, linha do tempo e controles.
struct PlaybackSection: View {
    @EnvironmentObject var model: AppModel
    let camera: Camera
    @StateObject private var vm: PlaybackModel
    @State private var showCalendar = false
    @State private var showExport = false
    @State private var fullscreen = false
    @State private var share: SharePayload?

    init(camera: Camera, api: APIClient) {
        self.camera = camera
        _vm = StateObject(wrappedValue: PlaybackModel(cameraId: camera.id, api: api))
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 10) {
                dayHeader
                player
                if let notice = vm.notice {
                    NoticeBanner(text: notice.text,
                                 actionTitle: notice.next.map { "Ir para \(Fmt.epoch($0, Fmt.hourMinute))" },
                                 action: notice.next == nil ? nil : { vm.goToNextAvailable() })
                }
                if let err = vm.error {
                    NoticeBanner(text: err, actionTitle: "Recarregar", action: { Task { await vm.open(day: vm.day, at: vm.cursor) } }, tint: .red)
                }
                timeline
                controls
                actions
                summary
            }
            .padding(.horizontal, 12)
            .padding(.bottom, 16)
        }
        .task {
            if vm.spans.isEmpty && vm.notice == nil {
                await vm.open(day: Date())
                await vm.loadMonth(Date())
            }
        }
        .onDisappear { vm.pause() }
        .sheet(isPresented: $showCalendar) {
            CalendarSheet(vm: vm) { day in
                showCalendar = false
                Task { await vm.open(day: day) }
            }
            .presentationDetents([.medium, .large])
        }
        .sheet(isPresented: $showExport) {
            ExportView(camera: camera, initialStart: max(vm.cursor - 300, vm.dayStart), initialEnd: min(vm.cursor + 300, Date().timeIntervalSince1970))
        }
        .fullScreenCover(isPresented: $fullscreen) {
            FullscreenPlayer(player: vm.player, title: camera.name, clock: { Date(timeIntervalSince1970: vm.cursor) })
        }
        .sheet(item: $share) { ShareSheet(items: $0.items) }
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

    private var player: some View {
        ZStack(alignment: .topLeading) {
            PlayerLayerView(player: vm.player)
            Text(Fmt.dateTime.string(from: Date(timeIntervalSince1970: vm.cursor)))
                .font(.caption.monospacedDigit().weight(.semibold))
                .padding(.horizontal, 6).padding(.vertical, 3)
                .background(.black.opacity(0.55), in: Capsule())
                .foregroundStyle(.white)
                .padding(8)
            if vm.isLoading {
                ProgressView().tint(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .aspectRatio(16 / 9, contentMode: .fit)
        .background(Color.black)
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .onTapGesture(count: 2) { fullscreen = true }
    }

    private var timeline: some View {
        VStack(spacing: 4) {
            HStack {
                Text(Fmt.epoch(vm.cursor, Fmt.time))
                    .font(.title2.monospacedDigit().weight(.semibold))
                if vm.isScrubbing {
                    Text(locationHint).font(.caption).foregroundStyle(.secondary)
                }
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

    private var locationHint: String {
        switch TimelineMath.locate(vm.cursor, in: vm.spans) {
        case .recorded: return "gravado"
        case .gap: return "sem gravação"
        }
    }

    private var controls: some View {
        HStack(spacing: 18) {
            Button { vm.skip(-60) } label: { Image(systemName: "gobackward.60") }.accessibilityLabel("Voltar 1 minuto")
            Button { vm.skip(-10) } label: { Image(systemName: "gobackward.10") }.accessibilityLabel("Voltar 10 segundos")
            Button { vm.togglePlay() } label: {
                Image(systemName: vm.isPlaying ? "pause.circle.fill" : "play.circle.fill").font(.system(size: 46))
            }
            .accessibilityLabel(vm.isPlaying ? "Pausar" : "Reproduzir")
            Button { vm.skip(10) } label: { Image(systemName: "goforward.10") }.accessibilityLabel("Avançar 10 segundos")
            Button { vm.skip(60) } label: { Image(systemName: "goforward.60") }.accessibilityLabel("Avançar 1 minuto")
        }
        .font(.title2)
        .padding(.vertical, 4)
    }

    private var actions: some View {
        HStack(spacing: 8) {
            Menu {
                ForEach([Float(1), 2, 4, 8, 16], id: \.self) { r in
                    Button("\(Int(r))×") { vm.setRate(r) }
                }
            } label: {
                Label("\(Int(vm.rate))×", systemImage: "speedometer")
            }
            .buttonStyle(.bordered)

            Button {
                if let img = vm.captureFrame() { share = SharePayload(items: [img]) }
            } label: { Label("Capturar", systemImage: "camera") }
            .buttonStyle(.bordered)

            Spacer()

            Button { showExport = true } label: { Label("Exportar", systemImage: "square.and.arrow.up") }
                .buttonStyle(.borderedProminent)
        }
        .font(.subheadline)
    }

    private var summary: some View {
        let total = TimelineMath.recordedSeconds(vm.spans)
        return HStack {
            Image(systemName: "film.stack")
            Text(total > 0 ? "Gravado neste dia: \(Fmt.duration(total)) em \(vm.spans.count) trecho(s)" : "Sem gravações neste dia")
            Spacer()
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }
}

/// Folha com o calendário mensal.
struct CalendarSheet: View {
    @ObservedObject var vm: PlaybackModel
    let onSelect: (Date) -> Void
    @State private var month = Date()

    var body: some View {
        NavigationStack {
            MonthCalendarView(month: $month, selected: vm.day, recorded: vm.recordedDays, onSelect: onSelect)
                .padding()
                .navigationTitle("Escolha o dia")
                .navigationBarTitleDisplayMode(.inline)
                .task(id: Fmt.isoMonth.string(from: month)) { await vm.loadMonth(month) }
                .onAppear { month = vm.day }
            Spacer()
        }
    }
}
