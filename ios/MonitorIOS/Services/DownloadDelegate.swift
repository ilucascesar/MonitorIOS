import Foundation

/// Delegate de download com progresso; move o arquivo para ``destination`` ao concluir.
final class DownloadDelegate: NSObject, URLSessionDownloadDelegate, @unchecked Sendable {
    let destination: URL
    let progress: @Sendable (Double) -> Void
    var continuation: CheckedContinuation<URL, Error>?
    private var moveError: Error?
    private var finished = false

    init(destination: URL, progress: @escaping @Sendable (Double) -> Void) {
        self.destination = destination
        self.progress = progress
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didWriteData bytesWritten: Int64,
                    totalBytesWritten: Int64, totalBytesExpectedToWrite: Int64) {
        if totalBytesExpectedToWrite > 0 {
            progress(Double(totalBytesWritten) / Double(totalBytesExpectedToWrite))
        }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask, didFinishDownloadingTo location: URL) {
        if let http = downloadTask.response as? HTTPURLResponse, !(200..<300).contains(http.statusCode) {
            moveError = APIError.server(status: http.statusCode, message: "Falha no download (\(http.statusCode))")
            return
        }
        do {
            try? FileManager.default.removeItem(at: destination)
            try FileManager.default.moveItem(at: location, to: destination)
            finished = true
        } catch {
            moveError = error
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let cont = continuation else { return }
        continuation = nil
        if let error {
            cont.resume(throwing: APIError.network(error.localizedDescription))
        } else if let moveError {
            cont.resume(throwing: moveError)
        } else if finished {
            progress(1)
            cont.resume(returning: destination)
        } else {
            cont.resume(throwing: APIError.network("download incompleto"))
        }
    }
}
