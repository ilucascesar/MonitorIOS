import XCTest
@testable import MonitorIOS

/// JSON no formato real devolvido pelo servidor (campos snake_case, extras ignorados).
final class DecodingTests: XCTestCase {
    private let api = APIClient()

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try api.decode(T.self, from: Data(json.utf8))
    }

    func testCamera() throws {
        let cam = try decode(Camera.self, """
        {"id": 3, "name": "Câmera 3", "host": "192.168.1.50", "onvif_port": 8899, "rtsp_port": 8554,
         "username": "admin", "has_password": true, "main_url": "rtsp://192.168.1.50:8554/stream0",
         "sub_url": "rtsp://192.168.1.50:8554/stream1", "record_stream": "main", "audio_mode": "off",
         "record_enabled": true, "live_enabled": true, "folder": "Camera_3", "codec": "h264",
         "width": 2304, "height": 1296, "fps": 15, "sub_codec": "h264", "sub_width": 640,
         "sub_height": 360, "sort_order": 0}
        """)
        XCTAssertEqual(cam.id, 3)
        XCTAssertEqual(cam.onvifPort, 8899)
        XCTAssertTrue(cam.hasSubstream)
        XCTAssertEqual(cam.resolutionText, "2304×1296")
        XCTAssertEqual(cam.fps, 15)
    }

    func testSystemStatus() throws {
        let st = try decode(SystemStatus.self, """
        {"server": {"version": "1.0.0", "time": 1760000000.5, "uptime_seconds": 3600, "cpu_percent": 4.2,
                    "memory_percent": 41.0, "process_memory_bytes": 81234567, "ffmpeg_processes": 3,
                    "process_cpu_percent": 1.5, "ffmpeg_memory_bytes": 90000000},
         "storage": {"status": "ok", "reason": "", "checked_at": 1760000000, "total_bytes": 34292629504,
                     "free_bytes": 25000000000, "used_by_system_bytes": 1200000000, "buffer_bytes": 0,
                     "upload_backlog": 0, "retention_days": 30, "max_storage_bytes": 19327352832,
                     "write_latency_ms": 35.2},
         "cameras": [{"id": 1, "name": "Câmera 1", "online": true, "recording": true, "record_enabled": true,
                      "live_enabled": true, "recorder_state": "recording", "last_seen": 1760000000,
                      "last_segment_end": 1759999990, "last_error": "", "restarts": 0, "reachable": true,
                      "live_active": false}],
         "events_version": 12}
        """)
        XCTAssertTrue(st.storage.isHealthy)
        XCTAssertEqual(st.storage.usedFraction!, 1 - 25000000000.0 / 34292629504.0, accuracy: 1e-9)
        XCTAssertEqual(st.cameras.first?.recorderState, "recording")
        XCTAssertEqual(st.server.uptimeSeconds, 3600)
    }

    func testStorageUnavailableWithNulls() throws {
        let s = try decode(StorageState.self, """
        {"status": "unavailable", "reason": "compartilhamento inacessível", "checked_at": null,
         "total_bytes": null, "free_bytes": null}
        """)
        XCTAssertFalse(s.isHealthy)
        XCTAssertNil(s.usedFraction)
    }

    func testTimelineSpans() throws {
        let tl = try decode(TimelineResponse.self, """
        {"camera_id": 1, "start": 0, "end": 86400, "intervals": [[10, 70], [100, 160]],
         "gaps": [[0, 10], [70, 100], [160, 86400]], "recorded_seconds": 120, "segments": 2}
        """)
        XCTAssertEqual(tl.spans, [TimeSpan(start: 10, end: 70), TimeSpan(start: 100, end: 160)])
    }

    func testLocateGap() throws {
        let loc = try decode(LocateResponse.self, """
        {"recorded": false, "interval": null, "next_available": 1760003600, "previous_end": 1760000000}
        """)
        XCTAssertFalse(loc.recorded)
        XCTAssertEqual(loc.nextAvailable, 1760003600)
    }

    func testExportJob() throws {
        let job = try decode(ExportJob.self, """
        {"id": "a1b2c3", "camera_id": 1, "start_ts": 100, "end_ts": 400, "status": "done", "progress": 1.0,
         "size": 4581021, "gaps": [[200, 230]], "error": "", "download_url": "/api/v1/exports/a1b2c3/download"}
        """)
        XCTAssertEqual(job.status, "done")
        XCTAssertEqual(job.gaps.count, 1)
        XCTAssertNotNil(job.downloadUrl)
    }

    func testNormalizeServerURL() {
        XCTAssertEqual(APIClient.normalize("100.113.162.101:8765/")?.absoluteString, "http://100.113.162.101:8765")
        XCTAssertEqual(APIClient.normalize(" https://nvr.tail1234.ts.net ")?.absoluteString, "https://nvr.tail1234.ts.net")
        XCTAssertNil(APIClient.normalize(""))
    }

    func testAbsoluteMediaURLKeepsQuery() {
        let c = APIClient()
        c.configure(baseURL: URL(string: "http://100.113.162.101:8765"), token: nil)
        let u = c.absoluteURL("/api/v1/live/1/sub/index.m3u8?mt=abc")
        XCTAssertEqual(u?.absoluteString, "http://100.113.162.101:8765/api/v1/live/1/sub/index.m3u8?mt=abc")
    }
}
