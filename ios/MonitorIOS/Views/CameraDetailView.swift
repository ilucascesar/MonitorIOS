import AVFoundation
import SwiftUI

struct CameraDetailView: View {
    @EnvironmentObject var model: AppModel
    let camera: Camera
    @State private var mode = 0  // 0 = ao vivo, 1 = gravações

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

    var body: some View {
        ScrollView {
            VStack(spacing: 12) {
                ZStack {
                    PlayerLayerView(player: live.player)
                    overlay
                }
                .aspectRatio(16 / 9, contentMode: .fit)
                .background(Color.black)
                .clipShape(RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).fill(.white.opacity(flash ? 0.6 : 0)).allowsHitTesting(false))
                .onTapGesture(count: 2) { fullscreen = true }

                HStack(spacing: 10) {
                    Menu {
                        Picker("Qualidade", selection: Binding(get: { live.quality }, set: { live.setQuality($0) })) {
                            Text("Alta (\(camera.resolutionText))").tag("main")
                            if camera.hasSubstream { Text("Econômica (\(camera.subWidth)×\(camera.subHeight))").tag("sub") }
                        }
                    } label: {
                        Label(live.quality == "main" ? "Alta" : "Econômica", systemImage: "dial.medium")
                    }
                    .buttonStyle(.bordered)

                    Button {
                        if let img = live.captureFrame() {
                            withAnimation(.easeOut(duration: 0.25)) { flash = true }
                            DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) { flash = false }
                            share = SharePayload(items: [img])
                        }
                    } label: {
                        Label("Capturar", systemImage: "camera.fill")
                    }
                    .buttonStyle(.bordered)
                    .disabled(live.state != .playing)

                    Spacer()
                    Button {
                        fullscreen = true
                    } label: {
                        Image(systemName: "arrow.up.left.and.arrow.down.right")
                    }
                    .buttonStyle(.borderedProminent)
                    .accessibilityLabel("Tela cheia")
                }

                infoCard
            }
            .padding(.horizontal, 12)
        }
        .onAppear { live.start(cameraId: camera.id, api: model.api) }
        .onDisappear { live.stop() }
        .fullScreenCover(isPresented: $fullscreen) {
            FullscreenPlayer(player: live.player, title: camera.name, clock: { live.now })
        }
        .sheet(item: $share) { ShareSheet(items: $0.items) }
    }

    @ViewBuilder private var overlay: some View {
        VStack {
            HStack {
                Text(Fmt.dateTime.string(from: live.now))
                    .font(.caption.monospacedDigit().weight(.semibold))
                    .padding(.horizontal, 6).padding(.vertical, 3)
                    .background(.black.opacity(0.55), in: Capsule())
                    .foregroundStyle(.white)
                Spacer()
                Label("AO VIVO", systemImage: "dot.radiowaves.left.and.right")
                    .font(.caption2.weight(.bold))
                    .padding(.horizontal, 6).padding(.vertical, 3)
                    .background(Color.red, in: Capsule())
                    .foregroundStyle(.white)
            }
            Spacer()
        }
        .padding(8)
        switch live.state {
        case .loading, .idle:
            ProgressView("Conectando…").tint(.white).foregroundStyle(.white)
        case .failed(let msg):
            VStack(spacing: 8) {
                Image(systemName: "video.slash.fill").font(.title)
                Text(msg).font(.footnote).multilineTextAlignment(.center)
                Button("Tentar agora") { live.retryNow() }.buttonStyle(.borderedProminent).controlSize(.small)
            }
            .foregroundStyle(.white)
            .padding()
        case .playing:
            EmptyView()
        }
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

// MARK: - Tela cheia

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
