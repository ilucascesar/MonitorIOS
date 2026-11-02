import SwiftUI

/// Exportação de um trecho: o servidor concatena os segmentos (sem recodificar) e o app baixa o MP4.
struct ExportView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let camera: Camera
    @State private var start: Date
    @State private var end: Date
    @StateObject private var exporter = ExportModel()
    @State private var share: SharePayload?

    init(camera: Camera, initialStart: Double, initialEnd: Double) {
        self.camera = camera
        _start = State(initialValue: Date(timeIntervalSince1970: initialStart))
        _end = State(initialValue: Date(timeIntervalSince1970: max(initialEnd, initialStart + 60)))
    }

    private var duration: Double { end.timeIntervalSince(start) }
    private var valid: Bool { duration > 0 && duration <= 6 * 3600 }

    var body: some View {
        NavigationStack {
            Form {
                Section("Câmera") {
                    Text(camera.name)
                }
                Section {
                    DatePicker("Início", selection: $start, in: ...Date(), displayedComponents: [.date, .hourAndMinute])
                    DatePicker("Fim", selection: $end, in: ...Date(), displayedComponents: [.date, .hourAndMinute])
                    LabeledContent("Duração", value: duration > 0 ? Fmt.duration(duration) + (duration < 60 ? " (\(Int(duration)) s)" : "") : "inválida")
                } header: {
                    Text("Intervalo")
                } footer: {
                    Text("Máximo de 6 horas. A qualidade original é preservada (sem recodificar o vídeo).")
                }

                Section {
                    switch exporter.phase {
                    case .idle:
                        Button {
                            exporter.start(cameraId: camera.id, cameraName: camera.name, start: start.timeIntervalSince1970,
                                           end: end.timeIntervalSince1970, api: model.api)
                        } label: {
                            Label("Exportar vídeo", systemImage: "square.and.arrow.up")
                        }
                        .disabled(!valid)
                    case .exporting(let p):
                        ProgressView(value: p) { Text("Preparando no servidor… \(Int(p * 100))%") }
                        Button("Cancelar", role: .cancel) { exporter.cancel() }
                    case .downloading(let p):
                        ProgressView(value: p) { Text("Baixando… \(Int(p * 100))%") }
                    case .done(let url):
                        Label("Vídeo pronto", systemImage: "checkmark.circle.fill").foregroundStyle(.green)
                        Button {
                            share = SharePayload(items: [url])
                        } label: {
                            Label("Salvar ou compartilhar", systemImage: "square.and.arrow.up.on.square")
                        }
                    case .failed(let msg):
                        Label(msg, systemImage: "xmark.octagon.fill").foregroundStyle(.red)
                        Button("Tentar novamente") {
                            exporter.start(cameraId: camera.id, cameraName: camera.name, start: start.timeIntervalSince1970,
                                           end: end.timeIntervalSince1970, api: model.api)
                        }
                    }
                    if let note = exporter.gapsNote {
                        Text(note).font(.footnote).foregroundStyle(.orange)
                    }
                } footer: {
                    Text("Use “Salvar vídeo” para a fototeca ou “Salvar em Arquivos”.")
                }

                Section {
                    Button {
                        Task { await exporter.protect(cameraId: camera.id, start: start.timeIntervalSince1970, end: end.timeIntervalSince1970, api: model.api) }
                    } label: {
                        Label("Marcar como importante", systemImage: "lock.shield")
                    }
                    .disabled(!valid)
                    if let msg = exporter.protectMessage {
                        Text(msg).font(.footnote).foregroundStyle(.secondary)
                    }
                } footer: {
                    Text("Gravações importantes não são apagadas pela limpeza automática.")
                }
            }
            .navigationTitle("Exportar trecho")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Fechar") {
                        if exporter.isBusy { exporter.cancel() }
                        dismiss()
                    }
                }
            }
            .sheet(item: $share) { ShareSheet(items: $0.items) }
        }
    }
}
