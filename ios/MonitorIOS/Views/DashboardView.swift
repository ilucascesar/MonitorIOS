import SwiftUI

struct DashboardView: View {
    @EnvironmentObject var model: AppModel
    @AppStorage("dashboard.columns") private var columns = 2
    // Ao vivo na grade por padrão: o servidor mantém as câmeras conectadas, então abre na hora.
    @AppStorage("dashboard.liveGrid.v2") private var liveGrid = true

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: 12) {
                    StatusHeader()
                    if model.cameras.isEmpty {
                        emptyState
                    } else {
                        LazyVGrid(columns: Array(repeating: GridItem(.flexible(), spacing: 10), count: max(1, columns)), spacing: 10) {
                            ForEach(model.cameras) { cam in
                                ZStack(alignment: .bottomTrailing) {
                                    NavigationLink(value: CameraRoute(camera: cam)) {
                                        CameraTile(camera: cam, state: model.state(for: cam.id), live: liveGrid && cam.liveEnabled)
                                    }
                                    .buttonStyle(.plain)
                                    // Atalho direto para as gravações da câmera.
                                    NavigationLink(value: CameraRoute(camera: cam, recordings: true)) {
                                        Image(systemName: "clock.arrow.circlepath")
                                            .font(.subheadline.weight(.semibold))
                                            .frame(width: 34, height: 34)
                                            .background(Color.accentColor.opacity(0.18), in: Circle())
                                    }
                                    .buttonStyle(.plain)
                                    .padding(6)
                                    .accessibilityLabel("Gravações de \(cam.name)")
                                }
                            }
                        }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.bottom, 12)
            }
            .refreshable { await model.refreshAll() }
            .navigationTitle("Câmeras")
            .navigationDestination(for: CameraRoute.self) { r in CameraDetailView(camera: r.camera, startInRecordings: r.recordings) }
            .toolbar {
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button {
                        liveGrid.toggle()
                    } label: {
                        Image(systemName: liveGrid ? "play.rectangle.fill" : "photo")
                    }
                    .accessibilityLabel(liveGrid ? "Mostrar miniaturas" : "Mostrar ao vivo na grade")
                    Button {
                        columns = columns == 1 ? 2 : 1
                    } label: {
                        Image(systemName: columns == 1 ? "square.grid.2x2" : "rectangle.grid.1x2")
                    }
                    .accessibilityLabel(columns == 1 ? "Grade" : "Uma câmera por linha")
                }
            }
        }
    }

    private var emptyState: some View {
        VStack(spacing: 10) {
            Image(systemName: "video.slash").font(.largeTitle).foregroundStyle(.secondary)
            Text("Nenhuma câmera").font(.headline)
            Text("Cadastre as câmeras no painel administrativo do servidor (http://<servidor>:8765/admin).")
                .font(.footnote).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .padding(.vertical, 60)
    }
}

/// Faixa com estado do servidor e do armazenamento.
struct StatusHeader: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        HStack(spacing: 8) {
            Pill(text: model.connection.label, systemImage: model.connection == .online ? "checkmark.circle.fill" : "wifi.exclamationmark",
                 color: model.connection == .online ? .green : .orange)
            if let st = model.status?.storage {
                Pill(text: storageText(st), systemImage: "externaldrive.fill", color: st.isHealthy ? .blue : .red)
            }
            Spacer(minLength: 0)
            if !model.network.isOnline {
                Pill(text: "Sem rede", systemImage: "antenna.radiowaves.left.and.right.slash", color: .red)
            }
        }
        .padding(.top, 4)
    }

    private func storageText(_ st: StorageState) -> String {
        guard st.isHealthy else { return st.status == "degraded" ? "Armazenamento com alerta" : "Pendrive indisponível" }
        if let free = st.freeBytes { return "\(Fmt.bytes(free)) livres" }
        return "Armazenamento OK"
    }
}

struct CameraTile: View {
    @EnvironmentObject var model: AppModel
    let camera: Camera
    let state: CameraState?
    let live: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ZStack(alignment: .topLeading) {
                Group {
                    if live {
                        MiniLivePlayer(cameraId: camera.id)
                    } else {
                        AuthImage(path: "/api/v1/cameras/\(camera.id)/snapshot.jpg", refresh: 30)
                    }
                }
                .aspectRatio(16 / 9, contentMode: .fit)
                .clipShape(UnevenRoundedRectangle(topLeadingRadius: 12, topTrailingRadius: 12))

                HStack {
                    if camera.recordEnabled { RecBadge(recording: state?.recording ?? false) }
                    Spacer()
                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                        .font(.caption.weight(.bold))
                        .padding(5)
                        .background(.black.opacity(0.5), in: Circle())
                        .foregroundStyle(.white)
                }
                .padding(6)
            }
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    StatusDot(online: state?.online ?? false)
                    Text(camera.name).font(.subheadline.weight(.semibold)).lineLimit(1)
                }
                Text(subtitle).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
            }
            .padding(8)
        }
        .background(Color(.secondarySystemBackground), in: RoundedRectangle(cornerRadius: 12))
    }

    private var subtitle: String {
        guard let state else { return "sem informação" }
        if state.online { return "Online · \(Fmt.relative(state.lastSeen))" }
        return "Offline · visto \(Fmt.relative(state.lastSeen))"
    }
}

/// Miniatura ao vivo da grade (mesma conexão permanente do servidor: abre na hora).
struct MiniLivePlayer: View {
    @EnvironmentObject var model: AppModel
    let cameraId: Int
    @StateObject private var live = LivePlayerModel(quality: "main")

    var body: some View {
        ZStack {
            LiveVideoView(view: live.renderView, gravity: .resizeAspectFill)
            if !live.hasImage {
                if case .failed = live.state {
                    Image(systemName: "exclamationmark.triangle").foregroundStyle(.yellow)
                } else {
                    ProgressView().tint(.white)
                }
            }
        }
        .onAppear { live.start(cameraId: cameraId, api: model.api) }
        .onDisappear { live.stop() }
    }
}
