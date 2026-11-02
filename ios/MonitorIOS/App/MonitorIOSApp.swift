import SwiftUI
import AVFoundation

@main
struct MonitorIOSApp: App {
    @StateObject private var model = AppModel()
    @Environment(\.scenePhase) private var scenePhase

    init() {
        // Vídeo das câmeras não deve interromper música de outros apps (áudio é opcional).
        try? AVAudioSession.sharedInstance().setCategory(.playback, mode: .moviePlayback, options: [.mixWithOthers])
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(model)
                .preferredColorScheme(model.colorScheme)
                .tint(Color.accentColor)
                .onAppear { model.start() }
        }
        .onChange(of: scenePhase) { _, phase in model.scenePhaseChanged(phase) }
    }
}

struct RootView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        Group {
            if model.isLoggedIn {
                TabView {
                    DashboardView()
                        .tabItem { Label("Câmeras", systemImage: "video.fill") }
                    RecordingsHubView()
                        .tabItem { Label("Gravações", systemImage: "calendar") }
                    SettingsView()
                        .tabItem { Label("Ajustes", systemImage: "gearshape.fill") }
                }
            } else {
                LoginView()
            }
        }
        .animation(.default, value: model.isLoggedIn)
    }
}
