import XCTest
@testable import MonitorIOS

final class TimelineMathTests: XCTestCase {
    private let spans = [
        TimeSpan(start: 1000, end: 1600),
        TimeSpan(start: 2000, end: 2300),
        TimeSpan(start: 5000, end: 5060),
    ]

    func testXAndTimeAreInverse() {
        let cursor = 43_200.0, span = 3600.0, width = 390.0
        XCTAssertEqual(TimelineMath.x(for: cursor, cursor: cursor, span: span, width: width), width / 2, accuracy: 1e-9)
        for t in [cursor - 1800, cursor - 17, cursor + 900, cursor + 1800] {
            let x = TimelineMath.x(for: t, cursor: cursor, span: span, width: width)
            XCTAssertEqual(TimelineMath.time(forX: x, cursor: cursor, span: span, width: width), t, accuracy: 1e-6)
        }
        XCTAssertEqual(TimelineMath.x(for: cursor - span / 2, cursor: cursor, span: span, width: width), 0, accuracy: 1e-9)
        XCTAssertEqual(TimelineMath.x(for: cursor + span / 2, cursor: cursor, span: span, width: width), width, accuracy: 1e-9)
    }

    func testTickStepKeepsMinimumSpacing() {
        let spans: [Double] = [300, 1800, 3600, 6 * 3600, 24 * 3600]
        for span in spans {
            let step = TimelineMath.tickStep(span: span, width: 390)
            XCTAssertTrue(TimelineMath.tickSteps.contains(step))
            if step != TimelineMath.tickSteps.last! {
                XCTAssertGreaterThanOrEqual(step * 390 / span, 56)
            }
        }
        XCTAssertLessThan(TimelineMath.tickStep(span: 300, width: 390), TimelineMath.tickStep(span: 86400, width: 390))
    }

    func testClampSpan() {
        XCTAssertEqual(TimelineMath.clampSpan(10), 300)
        XCTAssertEqual(TimelineMath.clampSpan(3600), 3600)
        XCTAssertEqual(TimelineMath.clampSpan(1e9), 86400)
    }

    func testLocateInsideRecording() {
        XCTAssertEqual(TimelineMath.locate(1000, in: spans), .recorded(spans[0]))
        XCTAssertEqual(TimelineMath.locate(2299.5, in: spans), .recorded(spans[1]))
    }

    func testLocateGapReportsNextAvailable() {
        XCTAssertEqual(TimelineMath.locate(500, in: spans), .gap(next: 1000, previousEnd: nil))
        XCTAssertEqual(TimelineMath.locate(1600, in: spans), .gap(next: 2000, previousEnd: 1600))
        XCTAssertEqual(TimelineMath.locate(3000, in: spans), .gap(next: 5000, previousEnd: 2300))
        XCTAssertEqual(TimelineMath.locate(9000, in: spans), .gap(next: nil, previousEnd: 5060))
        XCTAssertEqual(TimelineMath.locate(1, in: []), .gap(next: nil, previousEnd: nil))
    }

    func testPlaylistOffsetSkipsGaps() {
        XCTAssertEqual(TimelineMath.playlistOffset(for: 1000, in: spans), 0)
        XCTAssertEqual(TimelineMath.playlistOffset(for: 1300, in: spans), 300)
        XCTAssertEqual(TimelineMath.playlistOffset(for: 2000, in: spans), 600)
        XCTAssertEqual(TimelineMath.playlistOffset(for: 5030, in: spans), 600 + 300 + 30)
        XCTAssertNil(TimelineMath.playlistOffset(for: 1800, in: spans))
        XCTAssertNil(TimelineMath.playlistOffset(for: 99999, in: spans))
    }

    func testRecordedSeconds() {
        XCTAssertEqual(TimelineMath.recordedSeconds(spans), 960)
        XCTAssertEqual(TimelineMath.recordedSeconds([]), 0)
    }

    func testTimeSpanContainsIsHalfOpen() {
        let s = TimeSpan(start: 10, end: 20)
        XCTAssertTrue(s.contains(10))
        XCTAssertTrue(s.contains(19.999))
        XCTAssertFalse(s.contains(20))
        XCTAssertEqual(s.duration, 10)
    }
}
