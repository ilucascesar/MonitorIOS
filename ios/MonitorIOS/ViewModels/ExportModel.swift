import Foundation

@MainActor
final class ExportModel: ObservableObject {
    enum Phase: Equatable {
        case idle
        case exporting(Double)
        case downloading(Double)
        case done(URL)
        case failed(String)
    }

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var gapsNote: String?
    @Published private(set) var protectMessage: String?
    private var task: Task<Void, Never>?

    var isBusy: Bool {
        switch phase {
        case .exporting, .downloading: return true
        default: return false
        }
    }

    func start(cameraId: Int, cameraName: String, start: Double, end: Double, api: APIClient) {
        task?.cancel()
        gapsNote = nil
        task = Task { await run(cameraId: cameraId, cameraName: cameraName, start: start, end: end, api: api) }
    }

    func cancel() {
        task?.cancel()
        phase = .idle
    }

    private func run(cameraId: Int, cameraName: String, start: Double, end: Double, api: APIClient) async {
        phase = .exporting(0)
        do {
            var job = try await api.send(ExportJob.self, "/api/v1/exports", method: "POST",
                                         body: ["camera_id": cameraId, "start": start, "end": end])
            while job.status == "queued" || job.status == "running" {
                try await Task.sleep(nanoseconds: 1_000_000_000)
                job = try await api.send(ExportJob.self, "/api/v1/exports/\(job.id)")
                phase = .exporting(job.progress)
            }
            guard job.status == "done", let rel = job.downloadUrl, let url = api.absoluteURL(rel) else {
                phase = .failed(job.error.isEmpty ? "Falha na exportação" : job.error)
                return
            }
            if !job.gaps.isEmpty {
                let total = job.gaps.reduce(0.0) { $0 + (($1.count == 2) ? $1[1] - $1[0] : 0) }
                gapsNote = "O intervalo tem \(job.gaps.count) lacuna(s) sem gravação (\(Fmt.duration(total))); o vídeo salta esses trechos."
            }
            let safeName = cameraName.replacingOccurrences(of: "[^A-Za-z0-9_-]+", with: "_", options: .regularExpression)
            let name = "\(safeName)_\(Fmt.epoch(start, Fmt.isoDay))_\(Fmt.epoch(start, Fmt.hourMinute).replacingOccurrences(of: ":", with: "h")).mp4"
            phase = .downloading(0)
            let file = try await api.download(url, suggestedName: name) { p in
                Task { @MainActor [weak self] in
                    if case .downloading = self?.phase { self?.phase = .downloading(p) }
                }
            }
            phase = .done(file)
            _ = try? await api.data("/api/v1/exports/\(job.id)", method: "DELETE")
        } catch is CancellationError {
            phase = .idle
        } catch {
            phase = .failed((error as? LocalizedError)?.errorDescription ?? error.localizedDescription)
        }
    }

    func protect(cameraId: Int, start: Double, end: Double, api: APIClient) async {
        do {
            let data = try await api.data("/api/v1/cameras/\(cameraId)/protect", method: "POST",
                                          body: ["start": start, "end": end, "protected": true])
            let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            let n = obj?["segments"] as? Int ?? 0
            protectMessage = n > 0 ? "\(n) segmento(s) protegido(s) contra a exclusão automática." : "Nenhuma gravação no intervalo."
        } catch {
            protectMessage = (error as? LocalizedError)?.errorDescription ?? error.localizedDescription
        }
    }
}
