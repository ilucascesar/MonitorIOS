import SwiftUI

/// Aba "Gravações": escolhe a câmera e mostra calendário + linha do tempo,
/// ou mostra até 4 câmeras juntas no mesmo horário (opção salva no aparelho).
struct RecordingsHubView: View {
    @EnvironmentObject var model: AppModel
    @AppStorage("recordingsCamera") private var selectedId = 0
    @AppStorage("recordingsGrid") private var gridMode = false

    private var selected: Camera? {
        model.cameras.first { $0.id == selectedId } ?? model.cameras.first
    }

    private var showGrid: Bool { gridMode && model.cameras.count > 1 }

    var body: some View {
        NavigationStack {
            Group {
                if showGrid {
                    let cams = Array(model.cameras.prefix(4))
                    // .id força um novo grupo de players se a lista de câmeras mudar.
                    MultiPlaybackSection(cameras: cams, api: model.api) { cam in
                        selectedId = cam.id
                        gridMode = false
                    }
                    .id(cams.map { $0.id })
                } else if let cam = selected {
                    VStack(spacing: 0) {
                        Picker("Câmera", selection: Binding(get: { cam.id }, set: { selectedId = $0 })) {
                            ForEach(model.cameras) { c in Text(c.name).tag(c.id) }
                        }
                        .pickerStyle(.segmented)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 8)
                        .opacity(model.cameras.count > 1 ? 1 : 0)
                        .frame(height: model.cameras.count > 1 ? nil : 0)

                        // .id força um novo PlaybackModel ao trocar de câmera.
                        PlaybackSection(camera: cam, api: model.api)
                            .id(cam.id)
                    }
                } else {
                    ContentUnavailableView("Nenhuma câmera", systemImage: "video.slash",
                                           description: Text("Cadastre câmeras no painel do servidor."))
                }
            }
            .navigationTitle(showGrid ? "Gravações" : (selected?.name ?? "Gravações"))
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                if model.cameras.count > 1 {
                    ToolbarItem(placement: .topBarTrailing) {
                        Button { gridMode.toggle() } label: {
                            Image(systemName: gridMode ? "rectangle" : "square.grid.2x2")
                        }
                        .accessibilityLabel(gridMode ? "Ver uma câmera" : "Ver 4 câmeras juntas")
                    }
                }
            }
            .refreshable { await model.refreshCameras() }
        }
    }
}
