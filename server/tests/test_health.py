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


# --- troca de IP pelo DHCP: a câmera é reencontrada pelo MAC -----------------------------------

import pytest

from app.cameras import netfind
from app.database import Database


@pytest.fixture
def cam_db(tmp_path):
    db = Database(tmp_path / "t.db")
    cid = db.insert_camera({"name": "Rua", "host": "192.168.1.4", "rtsp_port": 8554, "onvif_port": 5000,
                            "main_url": "rtsp://192.168.1.4:8554/stream0", "sub_url": "rtsp://192.168.1.4:8554/stream1",
                            "folder": "Rua"})
    return db, cid


def make_svc(db, macs, changed):
    """``macs``: IP -> MAC da rede simulada."""
    svc = CameraService(None, db, FakeEvents())
    svc.mac_of = lambda ip: macs.get(ip, "")
    svc.find_host = lambda mac, near, port, probe, lookup: netfind.find_host(mac, near, port, lambda ip, p, t: ip in macs, lookup)
    svc.on_change = changed.append
    return svc


def test_learns_mac_then_follows_ip_change(cam_db):
    db, cid = cam_db
    macs = {"192.168.1.4": "aa-aa", "192.168.1.9": "bb-bb"}
    changed = []
    svc = make_svc(db, macs, changed)
    svc.check_reachability(db.get_camera(cid), probe=lambda: True)
    assert db.get_camera(cid)["mac"] == "aa-aa"
    # O roteador dá o .4 para outra câmera e a nossa vai para o .7: o .4 ainda responde, mas com outro MAC.
    macs.update({"192.168.1.4": "bb-bb", "192.168.1.7": "aa-aa"})
    svc.check_reachability(db.get_camera(cid), probe=lambda: True)
    cam = db.get_camera(cid)
    assert cam["host"] == "192.168.1.7" and cam["mac"] == "aa-aa"
    assert cam["main_url"] == "rtsp://192.168.1.7:8554/stream0" and cam["sub_url"] == "rtsp://192.168.1.7:8554/stream1"
    assert changed == [cid]


def test_no_move_when_mac_not_found_and_scans_are_rate_limited(cam_db):
    db, cid = cam_db
    db.update_camera(cid, {"mac": "aa-aa"})
    scans = []
    svc = make_svc(db, {}, [])
    svc.find_host = lambda *a: scans.append(a) or None
    for _ in range(3):
        svc.check_reachability(db.get_camera(cid), probe=lambda: False)
    assert db.get_camera(cid)["host"] == "192.168.1.4"
    assert len(scans) == 1


def test_manual_host_edit_forgets_old_mac(cam_db):
    db, cid = cam_db
    db.update_camera(cid, {"mac": "aa-aa"})
    svc = CameraService(None, db, FakeEvents())
    svc.update(cid, {"host": "192.168.1.20"})
    assert db.get_camera(cid)["mac"] == ""


def test_with_host_keeps_credentials_and_port():
    assert netfind.with_host("rtsp://u:p@10.0.0.4:554/a", "10.0.0.4", "10.0.0.5") == "rtsp://u:p@10.0.0.5:554/a"
    assert netfind.with_host("rtsp://10.0.0.4/a", "10.0.0.4", "10.0.0.5") == "rtsp://10.0.0.5/a"
    assert netfind.with_host("rtsp://other:554/a", "10.0.0.4", "10.0.0.5") == "rtsp://other:554/a"
    assert netfind.with_host("", "10.0.0.4", "10.0.0.5") == ""
