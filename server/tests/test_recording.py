"""Testes de integração com FFmpeg real.

A "câmera" aqui é um arquivo H.264 lido em tempo real (-re) — usado apenas no teste para
exercitar o mesmo pipeline do RTSP (gravação, segmentação, indexação, upload, exportação).
"""
import json
import subprocess
import time
from pathlib import Path

import pytest

from conftest import FFPROBE, needs_ffmpeg

pytestmark = needs_ffmpeg


def wait_for(cond, timeout, step=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(step)
    return False


def make_camera(runtime, source_video, name="Teste"):
    cam_id = runtime.db.insert_camera({"name": name, "host": "127.0.0.1", "folder": name, "main_url": "rtsp://127.0.0.1/x",
                                        "record_enabled": 1, "codec": "h264"})
    runtime.supervisor.input_overrides[cam_id] = (str(source_video), ["-re", "-stream_loop", "-1"])
    return cam_id


def segs(runtime, cam_id):
    return runtime.db.query("SELECT * FROM segments WHERE camera_id=? ORDER BY start_ts", (cam_id,))


def test_continuous_recording_index_upload_and_playlist(runtime, source_video):
    runtime.cfg.update({"segment_seconds": 10, "hls_part_seconds": 3})
    cam_id = make_camera(runtime, source_video)
    runtime.storage.check_health()
    assert runtime.storage.state["status"] == "ok"
    runtime.storage.start()
    runtime.supervisor.start()
    try:
        assert wait_for(lambda: len(segs(runtime, cam_id)) >= 3, 60), "nenhum segmento indexado"
        status = runtime.supervisor.status()[cam_id]
        assert status["state"] == "recording" and status["online"]
    finally:
        runtime.supervisor.stop()
    rows = segs(runtime, cam_id)
    ok = [r for r in rows if r["status"] == "ok"]
    assert len(ok) >= 3
    # Durações reais vindas do ffprobe, não do valor nominal
    for r in ok:
        assert 0.5 < r["duration"] < 25
        assert r["codec"] == "h264" and (r["width"], r["height"]) == (640, 360)
        parts = json.loads(r["parts"])
        assert parts[0][0] == 0 and sum(p[1] for p in parts) == r["size"]
    # Segmentos consecutivos da mesma execução são contíguos (sem lacunas artificiais)
    for a, b in zip(ok, ok[1:]):
        assert abs(b["start_ts"] - a["end_ts"]) < 1.6
    # Upload para o "pendrive" (pasta temporária) com verificação de tamanho
    assert wait_for(lambda: runtime.db.usage_bytes("local") == 0, 30)
    runtime.storage.stop()
    for r in segs(runtime, cam_id):
        assert r["location"] == "remote"
        p = runtime.storage.resolve(r)
        assert p.is_file() and p.stat().st_size == r["size"]
        assert p.relative_to(runtime.cfg.storage_root).parts[0] == "Teste"
    assert not any(runtime.cfg.buffer_dir.joinpath("Teste").glob("*.ts"))

    # Partes em byte-range começam em pacotes TS válidos (PAT)
    r = ok[1]
    data = runtime.storage.resolve(r).read_bytes()
    for off, length, _d in json.loads(r["parts"]):
        assert data[off] == 0x47


def test_offline_camera_retries_without_affecting_others(runtime, source_video):
    runtime.cfg.update({"segment_seconds": 10, "stall_timeout_seconds": 10})
    good = make_camera(runtime, source_video, "Boa")
    bad = runtime.db.insert_camera({"name": "Offline", "host": "192.0.2.1", "folder": "Offline",
                                     "main_url": "rtsp://192.0.2.1:554/none", "record_enabled": 1})
    runtime.supervisor.start()
    try:
        assert wait_for(lambda: len(segs(runtime, good)) >= 2, 60)
        st = runtime.supervisor.status()
        assert st[good]["state"] == "recording"
        assert st[bad]["state"] in ("starting", "retrying") and not st[bad]["online"]
        assert wait_for(lambda: runtime.supervisor.status()[bad]["restarts"] >= 1, 40)
        assert runtime.supervisor.status()[bad]["last_error"]
    finally:
        runtime.supervisor.stop()


def test_crash_recovery_and_orphan_reconciliation(runtime, source_video):
    runtime.cfg.update({"segment_seconds": 30, "stall_timeout_seconds": 10})
    cam_id = make_camera(runtime, source_video)
    runtime.supervisor.start()
    rec = runtime.supervisor.recorders[cam_id]
    assert wait_for(lambda: rec.state == "recording", 30)
    time.sleep(6)
    first_run = rec.run_id
    rec._proc.kill()  # simula crash: segmento em andamento fica sem fechamento
    assert wait_for(lambda: rec.run_id != first_run and rec.state == "recording", 40), "não reiniciou"
    runtime.supervisor.stop()
    orphan = [p for p in (runtime.cfg.buffer_dir / "Teste").glob(f"{first_run}_*.ts")]
    assert orphan, "esperava arquivo interrompido"
    # Executa a reconciliação considerando o arquivo antigo o suficiente
    runtime.cfg.update({"stall_timeout_seconds": 10})
    import os

    for p in orphan:
        os.utime(p, (time.time() - 60, time.time() - 60))
    n = runtime.supervisor.reconcile_buffer()
    assert n >= 1
    rows = [r for r in segs(runtime, cam_id) if r["run_id"] == first_run]
    assert rows and all(r["status"] in ("partial", "ok") for r in rows)
    assert any(r["status"] == "partial" and r["duration"] > 3 for r in rows)


def test_export_concatenates_with_trim(runtime, source_video):
    runtime.cfg.update({"segment_seconds": 10})
    cam_id = make_camera(runtime, source_video)
    runtime.supervisor.start()
    try:
        assert wait_for(lambda: len([r for r in segs(runtime, cam_id) if r["status"] == "ok"]) >= 3, 60)
    finally:
        runtime.supervisor.stop()
    rows = [r for r in segs(runtime, cam_id) if r["status"] == "ok"]
    start = rows[0]["start_ts"] + 2
    end = rows[2]["end_ts"] - 2
    runtime.exports.start()
    job = runtime.exports.create(cam_id, 1, start, end)
    assert wait_for(lambda: runtime.exports.get(job["id"])["status"] in ("done", "failed"), 60)
    done = runtime.exports.get(job["id"])
    assert done["status"] == "done", done["error"]
    out = runtime.exports.file_path(job["id"])
    dur = float(subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(out)],
                               capture_output=True, text=True).stdout.strip())
    expected = end - start
    assert abs(dur - expected) < 3.5, (dur, expected)  # corte alinhado a keyframe (GOP 1 s)
    runtime.exports.stop()


def test_storage_unavailable_keeps_buffer_then_flushes(runtime, source_video, tmp_path):
    runtime.cfg.update({"segment_seconds": 10, "storage_root": str(tmp_path / "naoexiste" / "x" / "MonitorIOS"),
                        "smb_share": str(tmp_path / "naoexiste")})
    cam_id = make_camera(runtime, source_video)
    assert runtime.storage.check_health()["status"] == "unavailable"
    runtime.storage.start()
    runtime.supervisor.start()
    try:
        assert wait_for(lambda: len(segs(runtime, cam_id)) >= 2, 60)
        assert all(r["location"] == "local" for r in segs(runtime, cam_id))
        # "Pendrive volta"
        (tmp_path / "naoexiste").mkdir()
        assert runtime.storage.check_health()["status"] == "ok"
        runtime.storage._wake_uploader.set()
        assert wait_for(lambda: any(r["location"] == "remote" for r in segs(runtime, cam_id)), 30)
    finally:
        runtime.supervisor.stop()
        runtime.storage.stop()
