import XCTest
@testable import MonitorIOS

/// Protocolo do ao vivo (mesmo formato de server/app/streaming/hub.py) e peças do player.
final class LiveStreamTests: XCTestCase {
    func testParseVideoMessage() throws {
        // tipo 2, keyframe, ts 2000 ms, cts -33 ms, 3 bytes de dados
        var bytes: [UInt8] = [2, 1, 0, 0, 0x07, 0xD0]
        bytes += withUnsafeBytes(of: Int32(-33).bigEndian, Array.init)
        bytes += [0xAA, 0xBB, 0xCC]
        let msg = try XCTUnwrap(LiveWire.parse(Data(bytes)))
        XCTAssertEqual(msg.kind, LiveWire.video)
        XCTAssertTrue(msg.isKey)
        XCTAssertFalse(msg.isCatchUp)
        XCTAssertEqual(msg.timestamp, 2000)
        XCTAssertEqual(msg.pts, 1967)
        XCTAssertEqual(msg.payload, Data([0xAA, 0xBB, 0xCC]))
        XCTAssertNil(LiveWire.parse(Data([2, 1, 0])))
    }

    func testParseSliceKeepsOffsets() throws {
        // Data recortada (startIndex != 0), como pode vir do URLSession.
        let full = Data([9, 9] + [4, 2, 0, 0, 0, 1, 0, 0, 0, 0, 0x55])
        let msg = try XCTUnwrap(LiveWire.parse(full.dropFirst(2)))
        XCTAssertEqual(msg.kind, LiveWire.audio)
        XCTAssertTrue(msg.isCatchUp)
        XCTAssertEqual(msg.timestamp, 1)
        XCTAssertEqual(msg.payload, Data([0x55]))
    }

    func testAvcCParameterSets() throws {
        let sps: [UInt8] = [0x67, 0x64, 0x00, 0x1F, 0xAC]
        let pps: [UInt8] = [0x68, 0xEB, 0xE3]
        let avcc: [UInt8] = [1, 0x64, 0, 0x1F, 0xFF, 0xE1, 0, UInt8(sps.count)] + sps + [1, 0, UInt8(pps.count)] + pps
        let parsed = try XCTUnwrap(ParameterSets.parse(codec: 1, record: avcc))
        XCTAssertEqual(parsed.lengthSize, 4)
        XCTAssertEqual(parsed.sets, [sps, pps])
        XCTAssertNil(ParameterSets.parse(codec: 1, record: [1, 0x64]))
    }

    func testMulawDecoding() {
        XCTAssertEqual(MulawAudioPlayer.decode(0xFF), 0)
        XCTAssertEqual(MulawAudioPlayer.decode(0x7F), 0)
        XCTAssertEqual(MulawAudioPlayer.decode(0x80), 32124.0 / 32768, accuracy: 1e-6)
        XCTAssertEqual(MulawAudioPlayer.decode(0x00), -32124.0 / 32768, accuracy: 1e-6)
        XCTAssertEqual(MulawAudioPlayer.table.count, 256)
    }

    func testJitterClockFollowsFastestPathAndAdapts() {
        var clock = JitterClock()
        // Quadros a 15 fps; a rede às vezes atrasa 200 ms.
        let t1 = clock.target(ptsSeconds: 10.000, arrival: 100.050)
        let t2 = clock.target(ptsSeconds: 10.067, arrival: 100.317)  // chegou atrasado
        let t3 = clock.target(ptsSeconds: 10.133, arrival: 100.183)
        XCTAssertEqual(t1, 100.050 + clock.margin, accuracy: 1e-9)
        // Base = menor atraso visto (50 ms): o ritmo segue o relógio da câmera.
        XCTAssertEqual(t2 - t1, 0.067, accuracy: 1e-9)
        XCTAssertEqual(t3 - t2, 0.066, accuracy: 1e-9)
        let before = clock.margin
        clock.frameWasLate(now: 100.4)
        XCTAssertGreaterThan(clock.margin, before)
        clock.relax(now: 110)
        XCTAssertLessThan(clock.margin, before + 0.03)
        clock.reset()
        XCTAssertEqual(clock.margin, 0.12, accuracy: 1e-9)
    }

    func testWebSocketURL() {
        let api = APIClient()
        api.configure(baseURL: URL(string: "http://100.71.0.5:8765"), token: "t")
        XCTAssertEqual(api.webSocketURL("/api/v1/live/3/ws", query: [URLQueryItem(name: "quality", value: "main")])?.absoluteString,
                       "ws://100.71.0.5:8765/api/v1/live/3/ws?quality=main")
        api.configure(baseURL: URL(string: "https://pc.tail.ts.net:8765"), token: nil)
        XCTAssertEqual(api.webSocketURL("/api/v1/live/1/ws")?.absoluteString, "wss://pc.tail.ts.net:8765/api/v1/live/1/ws")
    }
}
