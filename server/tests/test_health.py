from app.cameras.service import CameraService


class FakeEvents:
    def __init__(self):
        self.emitted = []

    def emit(self, level, kind, message, camera_id=None):
        self.emitted.append((level, message))


CAM = {"id": 1, "name": "Câmera 1", "host": "192.0.2.10", "rtsp_port": 8554, "onvif_port": 80,
       "main_url": "rtsp://192.0.2.10:8554/stream0"}


def run(svc, results):
    return [svc.check_reachability(CAM, probe=lambda r=r: r) for r in results]


def test_isolated_probe_failure_is_not_reported():
    ev = FakeEvents()
    svc = CameraService(None, None, ev)
    states = run(svc, [True, False, True, False, False, True])
    assert states == [True] * 6
    assert ev.emitted == []


def test_consecutive_failures_mark_unreachable_once_and_recover():
    ev = FakeEvents()
    svc = CameraService(None, None, ev)
    states = run(svc, [True, False, False, False, False, True])
    assert states == [True, True, True, False, False, True]
    assert [lvl for lvl, _ in ev.emitted] == ["warning", "info"]
    assert "não responde" in ev.emitted[0][1] and "voltou" in ev.emitted[1][1]


def test_unreachable_at_startup_without_event():
    ev = FakeEvents()
    svc = CameraService(None, None, ev)
    assert run(svc, [False]) == [False]
    assert ev.emitted == []
