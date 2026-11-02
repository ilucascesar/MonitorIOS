import SwiftUI

struct SettingsView: View {
    @EnvironmentObject var model: AppModel
    @State private var server = ""
    @State private var testResult: String?
    @State private var testing = false
    @State private var confirmLogout = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://100.x.y.z:8765", text: $server)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    HStack {
                        Button("Testar") { test() }.disabled(testing)
                        Spacer()
                        Button("Salvar") {
                            do {
                                let url = try model.saveServer(server)
                                server = url.absoluteString
                                testResult = "Endereço salvo."
                                Task { await model.refreshAll() }
                            } catch {
                                testResult = (error as? LocalizedError)?.errorDescription ?? "Endereço inválido"
                            }
                        }
                        .disabled(server == model.serverURLString)
                    }
                    .buttonStyle(.borderless)
                    if testing { ProgressView() }
                    if let testResult { Text(testResult).font(.footnote).foregroundStyle(.secondary) }
                    LabeledContent("Conexão", value: model.connection.label)
                    LabeledContent("Rede", value: model.network.isOnline ? model.network.interfaceName : "sem rede")
                } header: {
                    Text("Servidor")
                } footer: {
                    Text("Fora de casa, use o endereço Tailscale do servidor (100.x.y.z) com o Tailscale ativo no iPhone.")
                }

                if let st = model.status {
                    Section("Armazenamento") {
                        LabeledContent("Estado", value: storageLabel(st.storage))
                        if let reason = st.storage.reason, !reason.isEmpty, !st.storage.isHealthy {
                            Text(reason).font(.footnote).foregroundStyle(.orange)
                        }
                        if let frac = st.storage.usedFraction {
                            ProgressView(value: frac) {
                                Text("Disco: \(Fmt.bytes(st.storage.freeBytes)) livres de \(Fmt.bytes(st.storage.totalBytes))")
                                    .font(.footnote)
                            }
                            .tint(frac > 0.9 ? .red : .accentColor)
                        }
                        LabeledContent("Usado pelo MonitorIOS", value: Fmt.bytes(st.storage.usedBySystemBytes))
                        if let maxB = st.storage.maxStorageBytes, maxB > 0 {
                            LabeledContent("Limite configurado", value: Fmt.bytes(maxB))
                        }
                        if let days = st.storage.retentionDays {
                            LabeledContent("Retenção", value: "\(days) dias")
                        }
                        if let backlog = st.storage.uploadBacklog, backlog > 0 {
                            LabeledContent("Aguardando envio", value: "\(backlog) segmento(s) · \(Fmt.bytes(st.storage.bufferBytes))")
                        }
                    }

                    Section("Servidor") {
                        LabeledContent("Versão", value: st.server.version)
                        LabeledContent("Ligado há", value: Fmt.duration(st.server.uptimeSeconds))
                        LabeledContent("CPU", value: String(format: "%.0f%%", st.server.cpuPercent))
                        LabeledContent("Memória", value: String(format: "%.0f%%", st.server.memoryPercent))
                        LabeledContent("Câmeras gravando", value: "\(st.cameras.filter(\.recording).count) de \(st.cameras.count)")
                    }
                }

                Section("Aparência") {
                    Picker("Tema", selection: $model.theme) {
                        Text("Escuro").tag("dark")
                        Text("Automático").tag("system")
                        Text("Claro").tag("light")
                    }
                }

                Section {
                    LabeledContent("Usuário", value: model.user?.username ?? "—")
                    if let role = model.user?.role {
                        LabeledContent("Perfil", value: role == "admin" ? "Administrador" : "Visualizador")
                    }
                    Button("Sair", role: .destructive) { confirmLogout = true }
                } header: {
                    Text("Conta")
                } footer: {
                    Text("A sessão fica guardada no Keychain deste aparelho. A senha não é armazenada.")
                }
            }
            .navigationTitle("Ajustes")
            .onAppear { server = model.serverURLString }
            .refreshable { await model.refreshAll() }
            .confirmationDialog("Sair desta conta?", isPresented: $confirmLogout, titleVisibility: .visible) {
                Button("Sair", role: .destructive) { model.logout() }
            }
        }
    }

    private func storageLabel(_ s: StorageState) -> String {
        switch s.status {
        case "ok": return "Funcionando"
        case "degraded": return "Com problemas"
        case "unavailable": return "Indisponível (gravando localmente)"
        default: return s.status
        }
    }

    private func test() {
        testing = true
        testResult = nil
        Task {
            let r = await model.testServer(server)
            testing = false
            switch r {
            case .success(let msg): testResult = msg
            case .failure(let err): testResult = (err as? LocalizedError)?.errorDescription ?? err.localizedDescription
            }
        }
    }
}
