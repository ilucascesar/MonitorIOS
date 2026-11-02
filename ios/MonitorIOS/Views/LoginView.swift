import SwiftUI

struct LoginView: View {
    @EnvironmentObject var model: AppModel
    @State private var server = ""
    @State private var username = ""
    @State private var password = ""
    @State private var busy = false
    @State private var message: String?
    @State private var messageIsError = false
    @FocusState private var focus: Field?

    enum Field { case server, user, password }

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    VStack(spacing: 8) {
                        Image(systemName: "video.badge.checkmark")
                            .font(.system(size: 52, weight: .semibold))
                            .foregroundStyle(Color.accentColor)
                        Text("Monitor").font(.largeTitle.bold())
                        Text("Câmeras da casa, ao vivo e gravadas.").font(.subheadline).foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
                }
                .listRowBackground(Color.clear)

                Section {
                    TextField("http://100.113.162.101:8765", text: $server)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .focused($focus, equals: .server)
                    Button {
                        Task { await testServer() }
                    } label: {
                        Label("Testar conexão", systemImage: "antenna.radiowaves.left.and.right")
                    }
                    .disabled(busy)
                } header: {
                    Text("Servidor")
                } footer: {
                    Text("Use o IP do Tailscale para acessar de qualquer lugar, ou o IP local em casa. Pode ser alterado depois em Ajustes.")
                }

                Section("Conta") {
                    TextField("Usuário", text: $username)
                        .textContentType(.username)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .focused($focus, equals: .user)
                        .submitLabel(.next)
                        .onSubmit { focus = .password }
                    SecureField("Senha", text: $password)
                        .textContentType(.password)
                        .focused($focus, equals: .password)
                        .submitLabel(.go)
                        .onSubmit { Task { await login() } }
                }

                if let message {
                    Section {
                        Label(message, systemImage: messageIsError ? "xmark.octagon.fill" : "checkmark.circle.fill")
                            .foregroundStyle(messageIsError ? .red : .green)
                            .font(.footnote)
                    }
                }

                Section {
                    Button {
                        Task { await login() }
                    } label: {
                        HStack {
                            Spacer()
                            if busy { ProgressView() } else { Text("Entrar").bold() }
                            Spacer()
                        }
                    }
                    .disabled(busy || username.isEmpty || password.isEmpty)
                }
            }
            .navigationBarTitleDisplayMode(.inline)
            .onAppear { if server.isEmpty { server = model.serverURLString } }
        }
    }

    private func testServer() async {
        busy = true
        defer { busy = false }
        switch await model.testServer(server) {
        case .success(let text):
            message = text
            messageIsError = false
        case .failure(let err):
            message = (err as? LocalizedError)?.errorDescription ?? err.localizedDescription
            messageIsError = true
        }
    }

    private func login() async {
        guard !username.isEmpty, !password.isEmpty else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try model.saveServer(server)
            try await model.login(username: username.trimmingCharacters(in: .whitespaces), password: password)
            password = ""
        } catch {
            message = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
            messageIsError = true
        }
    }
}
