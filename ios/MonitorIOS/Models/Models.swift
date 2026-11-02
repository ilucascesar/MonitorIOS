import Foundation

// Modelos espelhando a API do servidor (JSON snake_case convertido automaticamente).

struct User: Codable, Equatable {
    let id: Int
    let username: String
    let role: String
}

struct LoginResponse: Decodable {
    let token: String
    let user: User
    let expiresAt: Double
}

struct Camera: Codable, Identifiable, Equatable, Hashable {
    let id: Int
    let name: String
    let host: String
    let onvifPort: Int
    let rtspPort: Int
    let hasPassword: Bool
    let mainUrl: String
    let subUrl: String
    let recordStream: String
    let recordEnabled: Bool
    let liveEnabled: Bool
    let folder: String
    let codec: String
    let width: Int
    let height: Int
    let fps: Double
    let subWidth: Int
    let subHeight: Int

    var hasSubstream: Bool { !subUrl.isEmpty }
    var resolutionText: String { width > 0 ? "\(width)×\(height)" : "—" }
}

struct CameraState: Codable, Identifiable, Equatable {
    let id: Int
    let name: String
    let online: Bool
    let recording: Bool
    let recordEnabled: Bool
    let liveEnabled: Bool
    let recorderState: String
    let lastSeen: Double?
    let lastSegmentEnd: Double?
    let lastError: String
    let restarts: Int
    let reachable: Bool?
    let liveActive: Bool
}

struct ServerMetrics: Codable, Equatable {
    let version: String
    let time: Double
    let uptimeSeconds: Double
    let cpuPercent: Double
    let memoryPercent: Double
    let processMemoryBytes: Double?
    let ffmpegProcesses: Int?
}

struct StorageState: Codable, Equatable {
    let status: String
    let reason: String?
    let checkedAt: Double?
    let totalBytes: Double?
    let freeBytes: Double?
    let usedBySystemBytes: Double?
    let bufferBytes: Double?
    let uploadBacklog: Int?
    let retentionDays: Int?
    let maxStorageBytes: Double?
    let writeLatencyMs: Double?

    var isHealthy: Bool { status == "ok" }
    var usedFraction: Double? {
        guard let total = totalBytes, let free = freeBytes, total > 0 else { return nil }
        return (total - free) / total
    }
}

struct SystemStatus: Codable, Equatable {
    let server: ServerMetrics
    let storage: StorageState
    let cameras: [CameraState]
    let eventsVersion: Int
}

struct StatusMessage: Decodable {
    let type: String
    let data: SystemStatus
}

/// Intervalo de tempo em segundos epoch (UTC).
struct TimeSpan: Equatable, Hashable {
    let start: Double
    let end: Double
    var duration: Double { end - start }
    func contains(_ t: Double) -> Bool { t >= start && t < end }
}

struct TimelineResponse: Decodable {
    let cameraId: Int
    let start: Double
    let end: Double
    let intervals: [[Double]]
    let gaps: [[Double]]
    let recordedSeconds: Double
    let segments: Int

    var spans: [TimeSpan] { intervals.compactMap { $0.count == 2 ? TimeSpan(start: $0[0], end: $0[1]) : nil } }
}

struct DayInfo: Decodable, Hashable {
    let date: String
    let recordedSeconds: Double
}

struct DaysResponse: Decodable {
    let cameraId: Int
    let month: String
    let days: [DayInfo]
}

struct LocateResponse: Decodable {
    let recorded: Bool
    let interval: [Double]?
    let nextAvailable: Double?
    let previousEnd: Double?
}

struct PlaybackInfo: Decodable {
    let playlistUrl: String
    let snapshotUrl: String
    let mediaToken: String
    let expiresAt: Double
    let intervals: [[Double]]
}

struct LiveInfo: Decodable {
    let playlistUrl: String
    let quality: String
    let mediaToken: String
    let expiresAt: Double
}

struct ExportJob: Decodable, Identifiable {
    let id: String
    let cameraId: Int
    let startTs: Double
    let endTs: Double
    let status: String
    let progress: Double
    let size: Int
    let gaps: [[Double]]
    let error: String
    let downloadUrl: String?
}

struct HealthResponse: Decodable {
    let ok: Bool
    let version: String
}
