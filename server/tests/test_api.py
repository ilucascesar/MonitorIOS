import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

ADMIN_PW = "senha-admin-123"


@pytest.fixture
def client(runtime):
    runtime.auth.create_user("admin", ADMIN_PW, "admin")
    runtime.auth.create_user("viewer", "senha-viewer-1", "viewer")
    app = create_app(runtime)
    with TestClient(app, client=("127.0.0.1", 50000)) as c:
        yield c


def login(c, user="admin", pw=ADMIN_PW):
    r = c.post("/api/v1/auth/login", json={"username": user, "password": pw, "device": "pytest"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def add_segments(runtime, cam_id, start, count, dur=60.0, gap_after=None, folder="cam"):
    root = runtime.cfg.buffer_dir / folder
    root.mkdir(parents=True, exist_ok=True)
    t = start
    for i in range(count):
        name = f"r1_{i:04d}.ts"
        (root / name).write_bytes(bytes([0x47]) + b"\x00" * 1879)
        runtime.db.insert_segment({
            "camera_id": cam_id, "run_id": "r1", "location": "local", "path": f"{folder}/{name}",
            "start_ts": t, "end_ts": t + dur, "duration": dur, "size": 1880, "codec": "h264",
            "width": 640, "height": 360, "parts": [[0, 940, dur / 2], [940, 940, dur / 2]],
        })
        t += dur
        if gap_after is not None and i == gap_after:
            t += 600


def test_health_is_public_but_api_requires_auth(client):
    assert client.get("/api/v1/health").json()["ok"] is True
    assert client.get("/api/v1/cameras").status_code == 401
    assert client.get("/api/v1/system/status", headers={"Authorization": "Bearer invalido"}).status_code == 401


def test_login_errors_and_rate_limit(client):
    for _ in range(5):
        assert client.post("/api/v1/auth/login", json={"username": "admin", "password": "errada"}).status_code == 401
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW})
    assert r.status_code == 429 and "Retry-After" in r.headers


def test_camera_crud_never_exposes_password(client, runtime):
    h = login(client)
    body = {"name": "Garagem", "host": "192.168.1.7", "onvif_port": 5000, "rtsp_port": 8554,
            "username": "admin", "password": "segredo123", "main_url": "rtsp://192.168.1.7:8554/stream0",
            "record_enabled": False}
    r = client.post("/api/v1/cameras", json=body, headers=h)
    assert r.status_code == 201, r.text
    cam = r.json()
    assert "segredo123" not in r.text and cam["has_password"] and cam["folder"] == "Garagem"
    r = client.patch(f"/api/v1/cameras/{cam['id']}", json={"name": "Garagem 2", "main_url": "rtsp://x:y@192.168.1.7:8554/s0"}, headers=h)
    assert r.status_code == 200 and r.json()["main_url"] == "rtsp://192.168.1.7:8554/s0"
    assert "segredo123" not in client.get("/api/v1/cameras", headers=h).text
    row = runtime.db.get_camera(cam["id"])
    assert "segredo123" not in str(row) and row["password_enc"].startswith(("dpapi:", "plain:"))
    assert client.post("/api/v1/cameras", json={"name": "x", "host": "a b"}, headers=h).status_code == 422
    assert client.delete(f"/api/v1/cameras/{cam['id']}", headers=h).status_code == 200
    assert client.get(f"/api/v1/cameras/{cam['id']}", headers=h).status_code == 404


def test_viewer_cannot_admin(client):
    h = login(client, "viewer", "senha-viewer-1")
    assert client.post("/api/v1/cameras", json={"name": "x", "host": "1.2.3.4"}, headers=h).status_code == 403
    assert client.get("/api/v1/admin/settings", headers=h).status_code == 403


def test_admin_blocked_from_remote_address(runtime):
    runtime.auth.create_user("admin", ADMIN_PW, "admin")
    with TestClient(create_app(runtime), client=("100.70.1.2", 40000)) as c:
        h = login(c)
        assert c.get("/api/v1/admin/settings", headers=h).status_code == 403
        assert c.get("/admin/").status_code == 403
        assert c.get("/api/v1/cameras", headers=h).status_code == 200  # app continua funcionando


def test_cookie_auth_requires_csrf_header(client):
    r = client.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW, "cookie": True})
    assert r.status_code == 200 and "token" not in r.json()
    assert client.get("/api/v1/auth/me").status_code == 200
    assert client.post("/api/v1/admin/storage/test").status_code == 403
    assert client.post("/api/v1/admin/storage/test", headers={"X-Requested-With": "monitor"}).status_code == 200


def test_timeline_days_locate_and_playback(client, runtime):
    h = login(client)
    cam_id = runtime.db.insert_camera({"name": "Cam", "host": "1.2.3.4", "folder": "cam", "record_enabled": 0})
    start = 1793502000.0 + 14 * 3600  # 01/11/2026 14:00 (São Paulo)
    add_segments(runtime, cam_id, start, 5, gap_after=1)

    tlr = client.get(f"/api/v1/cameras/{cam_id}/timeline", params={"start": start - 3600, "end": start + 7200}, headers=h).json()
    assert tlr["intervals"] == [[start, start + 120], [start + 720, start + 900]]
    assert [start + 120, start + 720] in tlr["gaps"]

    days = client.get(f"/api/v1/cameras/{cam_id}/days", params={"month": "2026-11"}, headers=h).json()
    assert days["days"] == [{"date": "2026-11-01", "recorded_seconds": 300.0}]

    loc = client.get(f"/api/v1/cameras/{cam_id}/locate", params={"t": start + 300}, headers=h).json()
    assert loc["recorded"] is False and loc["next_available"] == start + 720 and loc["previous_end"] == start + 120

    info = client.get(f"/api/v1/cameras/{cam_id}/playback", params={"start": start, "end": start + 3600}, headers=h).json()
    pl = client.get(info["playlist_url"])
    assert pl.status_code == 200 and pl.headers["content-type"].startswith("application/vnd.apple.mpegurl")
    text = pl.text
    assert text.count("#EXT-X-DISCONTINUITY") == 1 and "#EXT-X-PROGRAM-DATE-TIME" in text
    seg_url = next(l for l in text.splitlines() if l.startswith("/api/v1/media/"))
    r = client.get(seg_url, headers={"Range": "bytes=0-187"})
    assert r.status_code == 206 and len(r.content) == 188 and r.content[0] == 0x47
    # Sem token ou com token de outra câmera -> negado
    assert client.get(seg_url.split("?")[0]).status_code == 401
    other = runtime.db.insert_camera({"name": "Outra", "host": "1.2.3.5", "folder": "outra", "record_enabled": 0})
    info2 = client.get(f"/api/v1/cameras/{other}/playback", params={"start": start, "end": start + 60}, headers=h).json()
    assert client.get(seg_url.split("?")[0] + "?mt=" + info2["media_token"]).status_code == 401


def test_protect_marks_segments(client, runtime):
    h = login(client)
    cam_id = runtime.db.insert_camera({"name": "Cam", "host": "1.2.3.4", "folder": "cam", "record_enabled": 0})
    add_segments(runtime, cam_id, 1000.0, 3)
    r = client.post(f"/api/v1/cameras/{cam_id}/protect", json={"start": 1050, "end": 1110}, headers=h)
    assert r.json()["segments"] == 2
    pr = client.get(f"/api/v1/cameras/{cam_id}/protected", params={"start": 0, "end": 5000}, headers=h).json()
    assert pr["intervals"] == [[1000.0, 1120.0]]


def test_invalid_ranges_rejected(client, runtime):
    h = login(client)
    cam_id = runtime.db.insert_camera({"name": "Cam", "host": "1.2.3.4", "folder": "cam", "record_enabled": 0})
    assert client.get(f"/api/v1/cameras/{cam_id}/timeline", params={"start": 10, "end": 5}, headers=h).status_code == 422
    assert client.get(f"/api/v1/cameras/{cam_id}/timeline", params={"start": 0, "end": 10 * 86400}, headers=h).status_code == 422
    assert client.get(f"/api/v1/cameras/{cam_id}/days", params={"month": "2026-11", "tz": "Mars/Base"}, headers=h).status_code == 422
    r = client.get("/api/v1/cameras/999/timeline", params={"start": 0, "end": 10}, headers=h)
    assert r.status_code == 404 and r.json()["detail"] == "câmera não encontrada"  # acentos intactos


def test_access_log_does_not_contain_tokens(client, runtime, caplog):
    import logging

    h = login(client)
    cam_id = runtime.db.insert_camera({"name": "Cam", "host": "1.2.3.4", "folder": "cam", "record_enabled": 0})
    add_segments(runtime, cam_id, 1000.0, 1)
    with caplog.at_level(logging.DEBUG):
        info = client.get(f"/api/v1/cameras/{cam_id}/playback", params={"start": 900, "end": 1100}, headers=h).json()
        client.get(info["playlist_url"])
    # Ignora o log do próprio cliente de teste (httpx); verifica só o que o servidor registra.
    server_log = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("httpx"))
    assert "GET /api/v1/cameras/1/playback.m3u8 200" in server_log
    assert info["media_token"] not in server_log
    assert h["Authorization"].split()[1] not in server_log
