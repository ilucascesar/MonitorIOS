import os
import time

from app.storage.manager import MARKER


def put(runtime, cam_id, folder, name, start, dur=60, size=1000, protected=0, location="remote"):
    base = runtime.cfg.storage_root if location == "remote" else runtime.cfg.buffer_dir
    rel = f"{folder}/2026-01-01/00/{name}" if location == "remote" else f"{folder}/{name}"
    p = base / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\x47" * size)
    sid = runtime.db.insert_segment({
        "camera_id": cam_id, "location": location, "path": rel, "start_ts": start, "end_ts": start + dur,
        "duration": dur, "size": size, "protected": protected,
    })
    return sid, p


def setup_root(runtime):
    runtime.storage.check_health()
    assert (runtime.cfg.storage_root / MARKER).exists()


def test_age_retention_respects_protected_and_foreign_files(runtime):
    setup_root(runtime)
    cam = runtime.db.insert_camera({"name": "Cam", "host": "h", "folder": "Cam"})
    runtime.cfg.update({"retention_days": 7})
    old = time.time() - 10 * 86400
    s1, p1 = put(runtime, cam, "Cam", "a.ts", old)
    s2, p2 = put(runtime, cam, "Cam", "b.ts", old + 60, protected=1)
    s3, p3 = put(runtime, cam, "Cam", "c.ts", time.time() - 3600)
    # Arquivos que NÃO são do sistema: mesma pasta sem índice e pasta do Agent DVR ao lado
    untracked = runtime.cfg.storage_root / "Cam" / "2026-01-01" / "00" / "manual.ts"
    untracked.write_bytes(b"x")
    agent = runtime.cfg.storage_root.parent / "video" / "1_2026-01-01.mp4"
    agent.parent.mkdir(parents=True)
    agent.write_bytes(b"x")
    os_old = time.time() - 30 * 86400
    import os

    os.utime(agent, (os_old, os_old))

    res = runtime.storage.run_retention()
    assert res["age"] == 1
    assert not p1.exists() and p2.exists() and p3.exists()
    assert untracked.exists() and agent.exists()
    assert runtime.db.get_segment(s1) is None and runtime.db.get_segment(s2)


def test_global_quota_deletes_oldest_first(runtime):
    setup_root(runtime)
    cam = runtime.db.insert_camera({"name": "Cam", "host": "h", "folder": "Cam"})
    gb = 1024 ** 3
    runtime.cfg.update({"max_storage_gb": 2.5, "retention_days": 365})
    now = time.time()
    ids = []
    for i in range(4):
        sid, _ = put(runtime, cam, "Cam", f"{i}.ts", now - 4000 + i * 60, size=10)
        runtime.db.execute("UPDATE segments SET size=? WHERE id=?", (gb, sid))  # finge 1 GB cada
        ids.append(sid)
    res = runtime.storage.run_retention()
    assert res["quota"] == 2
    assert runtime.db.get_segment(ids[0]) is None and runtime.db.get_segment(ids[1]) is None
    assert runtime.db.get_segment(ids[2]) and runtime.db.get_segment(ids[3])


def test_no_deletion_without_marker(runtime):
    setup_root(runtime)
    cam = runtime.db.insert_camera({"name": "Cam", "host": "h", "folder": "Cam"})
    runtime.cfg.update({"retention_days": 1})
    sid, p = put(runtime, cam, "Cam", "a.ts", time.time() - 5 * 86400)
    (runtime.cfg.storage_root / MARKER).unlink()
    runtime.storage.run_retention()
    assert p.exists() and runtime.db.get_segment(sid)


def test_tampered_index_path_is_never_followed(runtime, tmp_path):
    setup_root(runtime)
    cam = runtime.db.insert_camera({"name": "Cam", "host": "h", "folder": "Cam"})
    victim = tmp_path / "importante.ts"
    victim.write_bytes(b"x")
    runtime.db.insert_segment({"camera_id": cam, "location": "remote", "path": "../../importante.ts",
                               "start_ts": 0, "end_ts": 60, "duration": 60, "size": 1})
    runtime.storage.run_retention()
    assert victim.exists()


def test_buffer_overflow_drops_oldest_local_and_reports(runtime):
    cam = runtime.db.insert_camera({"name": "Cam", "host": "h", "folder": "Cam"})
    runtime.cfg.update({"buffer_max_gb": 1.0})
    gb = 1024 ** 3
    ids = []
    for i in range(3):
        sid, _ = put(runtime, cam, "Cam", f"{i}.ts", 1000 + i * 60, size=10, location="local")
        runtime.db.execute("UPDATE segments SET size=? WHERE id=?", (gb // 2, sid))
        ids.append(sid)
    dropped = runtime.storage.enforce_buffer_limit()
    assert dropped >= 1 and runtime.db.get_segment(ids[0]) is None and runtime.db.get_segment(ids[2])
    ev = runtime.db.recent_events(5)
    assert any(e["level"] == "error" and "buffer" in e["message"] for e in ev)


def test_reconcile_remote_adds_unknown_and_marks_missing(runtime, source_video):
    import shutil

    setup_root(runtime)
    cam = runtime.db.insert_camera({"name": "Cam", "host": "h", "folder": "Cam"})
    d = runtime.cfg.storage_root / "Cam" / "2026-11-01" / "14"
    d.mkdir(parents=True)
    old = d / "abc123_20261101-143500.ts"
    shutil.copy(source_video, old)
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    shutil.copy(source_video, d / "abc123_20261101-143600.ts")  # recém-enviado: fica para o uploader
    sid, p = put(runtime, cam, "Cam", "gone.ts", 5000)
    p.unlink()
    res = runtime.storage.reconcile_remote()
    assert res == {"added": 1, "missing": 1}
    row = runtime.db.one("SELECT * FROM segments WHERE path LIKE '%abc123%'")
    assert row["status"] == "reconciled" and 19 < row["duration"] < 21
    assert runtime.db.get_segment(sid)["status"] == "missing"


def test_new_camera_triggers_reconcile_of_existing_videos(runtime, source_video):
    """Reinstalação: câmeras importadas de novo já têm vídeos no pendrive e devem aparecer na linha do tempo."""
    import shutil

    setup_root(runtime)
    runtime.storage.check_health()
    d = runtime.cfg.storage_root / "Camera_7" / "2026-11-01" / "21"
    d.mkdir(parents=True)
    f = d / "a1b2c3_20261101-210000.ts"
    shutil.copy(source_video, f)
    os.utime(f, (time.time() - 3600, time.time() - 3600))
    runtime.db.insert_camera({"name": "Câmera 7", "host": "h", "folder": "Camera_7", "record_enabled": 0})
    runtime.apply_camera_changes()
    deadline = time.time() + 30
    while time.time() < deadline and not runtime.db.one("SELECT 1 FROM segments WHERE path LIKE '%a1b2c3%'"):
        time.sleep(0.2)
    row = runtime.db.one("SELECT * FROM segments WHERE path LIKE '%a1b2c3%'")
    assert row and row["status"] == "reconciled"
