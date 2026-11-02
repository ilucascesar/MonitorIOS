"""Gravações do Agent DVR na linha do tempo: indexação somente leitura, prioridade das gravações
próprias, trechos remuxados para o HLS e exportação."""
import json
import os
import shutil
import subprocess
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.recordings.indexer import probe_segment
from app.storage.agentdvr import LOCATION, parse_name
from conftest import FFMPEG, FFPROBE, needs_ffmpeg

ADMIN_PW = "senha-admin-123"
AGENT_DIR = ("192.168.1.1", "volume(sda1)", "Gravacoes_Cameras", "Garagem")  # caminho real do Agent DVR


@pytest.fixture(scope="session")
def agent_mkv(tmp_path_factory):
    """MKV como o do Agent DVR: H.264 + áudio G.711 (somente para testes)."""
    if not FFMPEG:
        pytest.skip("FFmpeg não encontrado")
    out = tmp_path_factory.mktemp("adv") / "src.mkv"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
        "-t", "20", "-c:v", "libx264", "-preset", "veryfast", "-g", "15", "-pix_fmt", "yuv420p",
        "-c:a", "pcm_mulaw", str(out),
    ], check=True, timeout=120)
    return out


def agent_name(agent_id: int, start: float) -> str:
    return f"{agent_id}_{datetime.fromtimestamp(start).strftime('%d-%m-%Y_%H-%M-%S')}_000.mkv"


def setup_agent(runtime, agent_mkv, start, agent_id=7):
    objects = runtime.cfg.data_dir / "objects.json"
    objects.write_text(json.dumps({"cameras": [
        {"id": 7, "name": "192.168.1.8", "settings": {"onvifident": "http://192.168.1.8:80/onvif/device_service"}},
    ]}), encoding="utf-8")
    runtime.cfg.update({"agentdvr_objects": str(objects), "hls_part_seconds": 3})
    cam_dir = runtime.cfg.agentdvr_root.joinpath(*AGENT_DIR)
    (cam_dir / "thumbs").mkdir(parents=True, exist_ok=True)
    f = cam_dir / agent_name(agent_id, start)
    shutil.copy(agent_mkv, f)
    os.utime(f, (time.time() - 600, time.time() - 600))
    (cam_dir / "thumbs" / (f.stem + ".jpg")).write_bytes(b"\xff\xd8")
    return f


def test_parse_name():
    agent_id, start = parse_name("7_01-11-2026_22-04-55_153.mkv")
    assert agent_id == 7 and abs(start - (datetime(2026, 11, 1, 22, 4, 55).timestamp() + 0.153)) < 1e-6
    assert parse_name("6_2026-11-01_17-04-36_215.mp4") is None and parse_name("x.mkv") is None


@needs_ffmpeg
def test_scan_is_read_only_and_retention_never_deletes(runtime, agent_mkv):
    runtime.storage.check_health()
    cam = runtime.db.insert_camera({"name": "Garagem", "host": "192.168.1.8", "folder": "Camera_8", "record_enabled": 0})
    start = time.time() - 3600
    f = setup_agent(runtime, agent_mkv, start)
    unknown = setup_agent(runtime, agent_mkv, start, agent_id=9)  # câmera que não está no MonitorIOS
    fresh = f.with_name(agent_name(7, time.time() - 30))
    shutil.copy(agent_mkv, fresh)  # ainda sendo gravado pelo Agent DVR

    assert runtime.storage.agentdvr.scan()["added"] == 1
    rows = runtime.db.query("SELECT * FROM segments WHERE location=?", (LOCATION,))
    assert len(rows) == 1
    seg = rows[0]
    assert seg["camera_id"] == cam and seg["container"] == "mkv" and seg["codec"] == "h264"
    assert abs(seg["start_ts"] - int(start)) < 1 and 19 < seg["duration"] < 21
    assert len(json.loads(seg["parts"])) >= 5 and runtime.storage.resolve(seg) == f
    assert runtime.storage.agentdvr.scan()["added"] == 0  # idempotente

    # Não conta na cota, não aparece como "última gravação" e nunca é apagado pela retenção.
    assert runtime.db.usage_bytes() == 0 and runtime.db.latest_segment(cam) is None
    runtime.db.execute("UPDATE segments SET start_ts=start_ts-90*86400, end_ts=end_ts-90*86400")
    runtime.cfg.update({"retention_days": 1, "max_storage_gb": 0.000001})
    runtime.storage.run_retention()
    assert f.exists() and unknown.exists() and fresh.exists()
    assert f.with_name("thumbs").joinpath(f.stem + ".jpg").exists()

    # Índice velho sai do banco (só a linha); arquivo removido pelo Agent DVR também.
    assert runtime.storage.agentdvr.scan()["removed"] == 1 and f.exists()
    runtime.cfg.update({"retention_days": 30})
    assert runtime.storage.agentdvr.scan()["added"] == 1
    f.unlink()
    assert runtime.storage.agentdvr.scan()["removed"] == 1
    assert not runtime.db.query("SELECT 1 FROM segments WHERE location=?", (LOCATION,))


def add_own(runtime, cam_id, start, dur):
    root = runtime.cfg.buffer_dir / "Camera_8"
    root.mkdir(parents=True, exist_ok=True)
    (root / "own.ts").write_bytes(bytes([0x47]) + b"\x00" * 1879)
    runtime.db.insert_segment({
        "camera_id": cam_id, "run_id": "r1", "location": "local", "path": "Camera_8/own.ts",
        "start_ts": start, "end_ts": start + dur, "duration": dur, "size": 1880, "codec": "h264",
        "width": 640, "height": 360, "parts": [[0, 1880, dur]],
    })


@needs_ffmpeg
def test_playlist_prefers_own_recording_and_serves_remuxed_parts(runtime, agent_mkv):
    runtime.storage.check_health()
    runtime.auth.create_user("admin", ADMIN_PW, "admin")
    cam = runtime.db.insert_camera({"name": "Garagem", "host": "192.168.1.8", "folder": "Camera_8", "record_enabled": 0})
    start = float(int(time.time() - 3600))
    setup_agent(runtime, agent_mkv, start)
    runtime.storage.agentdvr.scan()
    seg = runtime.db.one("SELECT * FROM segments WHERE location=?", (LOCATION,))
    add_own(runtime, cam, seg["start_ts"] + 10, 30)  # o MonitorIOS começou a gravar no meio do arquivo

    with TestClient(create_app(runtime), client=("127.0.0.1", 50000)) as c:
        r = c.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW, "device": "pytest"})
        h = {"Authorization": f"Bearer {r.json()['token']}"}
        tl = c.get(f"/api/v1/cameras/{cam}/timeline", params={"start": start - 60, "end": start + 120}, headers=h).json()
        assert len(tl["intervals"]) == 1 and tl["intervals"][0][1] == pytest.approx(seg["start_ts"] + 40, abs=0.01)

        info = c.get(f"/api/v1/cameras/{cam}/playback", params={"start": start - 60, "end": start + 120}, headers=h).json()
        text = c.get(info["playlist_url"]).text
        lines = text.splitlines()
        legacy = [l for l in lines if l.startswith(f"/api/v1/media/{seg['id']}/")]
        own = [l for l in lines if l.startswith("/api/v1/media/") and l not in legacy]
        assert legacy and own
        # Só as partes do Agent DVR antes da gravação própria; a própria vem depois, com descontinuidade.
        parts = json.loads(seg["parts"])
        t0 = parts[0][0]
        allowed = {i for i, p in enumerate(parts) if p[0] - t0 + p[2] / 2 < 10}
        assert {int(l.split("/")[-1].split(".")[0]) for l in legacy} == allowed
        assert lines.index(own[0]) > lines.index(legacy[-1]) and "#EXT-X-DISCONTINUITY" in text

        part = c.get(legacy[1])
        assert part.status_code == 200 and part.headers["content-type"] == "video/mp2t" and part.content[0] == 0x47
        assert c.get(legacy[1].split("?")[0]).status_code == 401  # exige token de mídia
        out = runtime.cfg.cache_dir / "part.ts"
        out.write_bytes(part.content)
        streams = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "csv=p=0", str(out)],
                                 capture_output=True, text=True).stdout.split()
        assert set(streams) == {"h264,video"}  # áudio G.711 descartado (não toca no iPhone)
        assert c.get(f"/api/v1/media/{seg['id']}/{len(parts)}.ts?mt={info['media_token']}").status_code == 404


@needs_ffmpeg
def test_export_of_agent_dvr_recording(runtime, agent_mkv, source_video):
    runtime.storage.check_health()
    cam = runtime.db.insert_camera({"name": "Garagem", "host": "192.168.1.8", "folder": "Camera_8", "record_enabled": 0})
    start = float(int(time.time() - 3600))
    setup_agent(runtime, agent_mkv, start)
    runtime.storage.agentdvr.scan()
    seg = runtime.db.one("SELECT * FROM segments WHERE location=?", (LOCATION,))
    # Gravação própria real logo depois do arquivo do Agent DVR, sobrepondo os 5 s finais.
    own_dir = runtime.cfg.buffer_dir / "Camera_8"
    own_dir.mkdir(parents=True, exist_ok=True)
    own = own_dir / "r2_0001.ts"
    shutil.copy(source_video, own)
    res = probe_segment(FFPROBE, own, 3)
    own_start = seg["end_ts"] - 5
    runtime.db.insert_segment({
        "camera_id": cam, "run_id": "r2", "location": "local", "path": "Camera_8/r2_0001.ts", "start_ts": own_start,
        "end_ts": own_start + res.duration, "duration": res.duration, "size": res.size, "codec": res.codec,
        "width": res.width, "height": res.height, "parts": res.parts,
    })
    runtime.exports.start()
    try:
        for a, b, audio in ((seg["start_ts"] + 2, seg["start_ts"] + 12, True), (seg["start_ts"] + 5, own_start + 10, False)):
            job = runtime.exports.create(cam, 1, a, b)
            deadline = time.time() + 60
            while runtime.exports.get(job["id"])["status"] not in ("done", "failed") and time.time() < deadline:
                time.sleep(0.3)
            done = runtime.exports.get(job["id"])
            assert done["status"] == "done", done["error"]
            out = runtime.exports.file_path(job["id"])
            probe = json.loads(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                                               "-of", "json", str(out)], capture_output=True, text=True).stdout)
            assert abs(float(probe["format"]["duration"]) - (b - a)) < 2.5, (probe, b - a)
            assert ("audio" in {s["codec_type"] for s in probe["streams"]}) is audio
    finally:
        runtime.exports.stop()
    assert runtime.storage.resolve(seg).exists()
