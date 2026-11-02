import AVFoundation
import SwiftUI

/// Rota de navegação para uma câmera (ao vivo ou direto nas gravações).
struct CameraRoute: Hashable {
    let camera: Camera
    var recordings = false
}

struct CameraDetailView: View {
    @EnvironmentObject var model: AppModel
    let camera: Camera
    @State private var mode: Int  // 0 = ao vivo, 1 = gravações

    init(camera: Camera, startInRecordings: Bool = false) {
        self.camera = camera
        _mode = State(initialValue: startInRecordings ? 1 : 0)
    }

    var body: some View {
        VStack(spacing: 0) {
            Picker("Modo", selection: $mode) {
                Text("AO VIVO").tag(0)
                Text("GRAVAÇÕES").tag(1)
            }
            .pickerStyle(.segmented)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)

            if mode == 0 {
                LiveSection(camera: camera)
            } else {
                PlaybackSection(camera: camera, api: model.api)
            }
        }
        .navigationTitle(camera.name)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                if let st = model.state(for: camera.id) {
                    HStack(spacing: 6) {
                        StatusDot(online: st.online)
                        Text(st.online ? "Online" : "Offline").font(.caption)
                    }
                }
            }
        }
    }
}

// MARK: - Ao vivo

struct LiveSection: View {
    @EnvironmentObject var model: AppModel
    let camera: Camera
    @StateObject private var live = LivePlayerModel(quality: "main")
    @State private var fullscreen = false
    @State private var share: SharePayload?
    @State private var flash = false

    private var aspect: CGFloat {
        let s = live.videoSize
        return s.width > 0 && s.height > 0 ? s.width / s.height : 16 / 9
    }

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                ZStack {
                    ZoomableLiveVideoView(view: live.renderView)
                    LiveOverlay(live: live, title: nil)
                }
                .aspectRatio(aspect, contentMode: .fit)
                .background(Color.black)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).fill(.white.opacity(flash ? 0.6 : 0)).allowsHitTesting(false))

                HStack(spacing: 10) {
                    Menu {
                        Picker("Qualidade", selection: Binding(get: { live.quality }, set: { live.setQuality($0) })) {
                            Text("Alta (\(camera.resolutionText))").tag("main")
                            if camera.hasSubstream { Text("Econômica (\(camera.subWidth)×\(camera.subHeight))").tag("sub") }
                        }
                    } label: {
                        Label(live.quality == "main" ? "HD" : "SD", systemImage: "dial.medium")
                    }
                    .buttonStyle(.bordered)

                    if live.hasAudio {
                        Button { live.toggleMute() } label: {
                            Image(systemName: live.isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill")
                        }
                        .buttonStyle(.bordered)
                        .accessibilityLabel(live.isMuted ? "Ligar áudio" : "Desligar áudio")
                    }

                    Button {
                        if let img = live.captureFrame() {
                            withAnimation(.easeOut(duration: 0.25)) { flash = true }
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { flash = false }
                            share = SharePayload(items: [img])
                        }
                    } label: {
                        Image(systemName: "camera.fill")
                    }
                    .buttonStyle(.bordered)
                    .disabled(!live.hasImage)
                    .accessibilityLabel("Capturar imagem")

                    Spacer()
                    Button {
                        fullscreen = true
                    } label: {
                        Image(systemName: "arrow.up.left.and.arrow.down.right")
                    }
                    .buttonStyle(.borderedProminent)
                    .accessibilityLabel("Tela cheia")
                }

                Text("Pinça ou toque duplo para ampliar.")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)

                infoCard
            }
            .padding(.horizontal, 12)
        }
        .onAppear { live.start(cameraId: camera.id, api: model.api) }
        .onDisappear { live.stop() }
        .onChange(of: fullscreen) { _, on in
            // A tela cheia abre a própria conexão (instantânea: o servidor já está conectado).
            if on { live.stop() } else { live.start(cameraId: camera.id, api: model.api) }
        }
        .fullScreenCover(isPresented: $fullscreen) {
            FullscreenLiveView(camera: camera, quality: live.quality)
        }
        .sheet(item: $share) { ShareSheet(items: $0.items) }
    }

    private var infoCard: some View {
        Card {
            VStack(alignment: .leading, spacing: 6) {
                let st = model.state(for: camera.id)
                LabeledContent("Conexão", value: (st?.online ?? false) ? "Online" : "Offline")
                LabeledContent("Gravação", value: camera.recordEnabled ? ((st?.recording ?? false) ? "Gravando 24h" : "Parada") : "Desativada")
                LabeledContent("Última comunicação", value: Fmt.relative(st?.lastSeen))
                LabeledContent("Vídeo", value: "\(camera.resolutionText) · \(camera.codec.uppercased()) · \(Int(camera.fps)) fps")
                if let err = st?.lastError, !err.isEmpty {
                    Text(err).font(.caption2).foregroundStyle(.orange)
                }
            }
            .font(.footnote)
        }
    }
}

/// Selo AO VIVO, relógio e estados de conexão sobre o vídeo. Não bloqueia os gestos de zoom.
struct LiveOverlay: View {
    @ObservedObject var live: LivePlayerModel
    let title: String?

    var body: some View {
        ZStack {
            VStack {
                HStack(spacing: 6) {
                    if let title {
                        Text(title).font(.caption.weight(.semibold))
                            .padding(.horizontal, 6).padding(.vertical, 3)
                            .background(.black.opacity(0.55), in: Capsule())
                    }
                    Spacer()
                    Label("AO VIVO", systemImage: "dot.radiowaves.left.and.right")
                        .font(.caption2.weight(.bold))
                        .padding(.horizontal, 6).padding(.vertical, 3)
                        .background(live.state == .playing ? Color.red : Color.gray, in: Capsule())
                }
                .foregroundStyle(.white)
                Spacer()
                if live.hasImage, live.state != .playing {
                    Label("Reconectando…", systemImage: "arrow.triangle.2.circlepath")
                        .font(.caption2.weight(.semibold))
                        .padding(.horizontal, 8).padding(.vertical, 4)
                        .background(.black.opacity(0.6), in: Capsule())
                        .foregroundStyle(.white)
                }
            }
            .padding(8)
            .allowsHitTesting(false)

            if !live.hasImage {
                switch live.state {
                case .failed(let msg):
                    VStack(spacing: 8) {
                        Image(systemName: "video.slash.fill").font(.title)
                        Text(msg).font(.footnote).multilineTextAlignment(.center)
                        Button("Tentar agora") { live.retryNow() }.buttonStyle(.borderedProminent).controlSize(.small)
                    }
                    .foregroundStyle(.white)
                    .padding()
                default:
                    ProgressView("Conectando…").tint(.white).foregroundStyle(.white).allowsHitTesting(false)
                }
            }
        }
    }
}

// MARK: - Tela cheia

/// Ao vivo em tela cheia (gire o iPhone para paisagem), com zoom e áudio.
struct FullscreenLiveView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let camera: Camera
    @StateObject private var live: LivePlayerModel
    @State private var share: SharePayload?

    init(camera: Camera, quality: String) {
        self.camera = camera
        _live = StateObject(wrappedValue: LivePlayerModel(quality: quality))
    }

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()
            ZoomableLiveVideoView(view: live.renderView).ignoresSafeArea()
            LiveOverlay(live: live, title: camera.name)
                .padding(.top, 44)
            VStack {
                HStack(spacing: 10) {
                    Spacer()
                    if live.hasAudio {
                        circleButton(live.isMuted ? "speaker.slash.fill" : "speaker.wave.2.fill",
                                     label: live.isMuted ? "Ligar áudio" : "Desligar áudio") { live.toggleMute() }
                    }
                    circleButton("camera.fill", label: "Capturar imagem") {
                        if let img = live.captureFrame() { share = SharePayload(items: [img]) }
                    }
                    circleButton("xmark", label: "Fechar") { dismiss() }
                }
                Spacer()
            }
            .padding()
        }
        .statusBarHidden()
        .persistentSystemOverlays(.hidden)
        .onAppear { live.start(cameraId: camera.id, api: model.api) }
        .onDisappear { live.stop() }
        .sheet(item: $share) { ShareSheet(items: $0.items) }
    }

    private func circleButton(_ icon: String, label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: icon).font(.headline).padding(10).background(.black.opacity(0.5), in: Circle())
        }
        .foregroundStyle(.white)
        .accessibilityLabel(label)
    }
}

/// Gravações em tela cheia (AVPlayer).
struct FullscreenPlayer: View {
    @Environment(\.dismiss) private var dismiss
    let player: AVPlayer
    let title: String
    let clock: () -> Date

    var body: some View {
        ZStack(alignment: .topLeading) {
            Color.black.ignoresSafeArea()
            PlayerLayerView(player: player).ignoresSafeArea()
            HStack {
                VStack(alignment: .leading) {
                    Text(title).font(.headline)
                    TimelineView(.periodic(from: .now, by: 0.5)) { _ in
                        Text(Fmt.dateTime.string(from: clock())).font(.caption.monospacedDigit())
                    }
                }
                .padding(8)
                .background(.black.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
                Spacer()
                Button {
                    dismiss()
                } label: {
                    Image(systemName: "xmark").font(.headline).padding(10).background(.black.opacity(0.5), in: Circle())
                }
                .accessibilityLabel("Fechar")
            }
            .foregroundStyle(.white)
            .padding()
        }
        .statusBarHidden()
    }
}
