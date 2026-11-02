import SwiftUI

/// Aba "Gravações": escolhe a câmera e mostra calendário + linha do tempo.
struct RecordingsHubView: View {
    @EnvironmentObject var model: AppModel
    @AppStorage("recordingsCamera") private var selectedId = 0

    private var selected: Camera? {
        model.cameras.first { $0.id == selectedId } ?? model.cameras.first
    }

    var body: some View {
        NavigationStack {
            Group {
                if let cam = selected {
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
            .navigationTitle(selected?.name ?? "Gravações")
            .navigationBarTitleDisplayMode(.inline)
            .refreshable { await model.refreshCameras() }
        }
    }
}
