"""Ao vivo de baixa latência (WebSocket), cópia local das gravações e migração de configuração."""
import asyncio
import json
import os
import struct
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Config
from app.main import create_app
from app.streaming import hub as H
from conftest import FFMPEG, needs_ffmpeg

ADMIN_PW = "senha-admin-123"


@pytest.fixture(scope="session")
def av_source(tmp_path_factory):
    """Câmera simulada para testes: H.264 com keyframe a cada 1 s + áudio G.711 (como as câmeras reais)."""
    if not FFMPEG:
        pytest.skip("FFmpeg não encontrado")
    out = tmp_path_factory.mktemp("cam") / "cam.mkv"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000",
        "-t", "10", "-c:v", "libx264", "-preset", "veryfast", "-g", "15", "-bf", "0", "-pix_fmt", "yuv420p",
        "-c:a", "pcm_mulaw", str(out),
    ], check=True, timeout=120)
    return out


def setup_cam(runtime, av_source):
    runtime.auth.create_user("admin", ADMIN_PW, "admin")
    cam = runtime.db.insert_camera({"name": "Garagem", "host": "10.0.0.8", "folder": "Garagem", "record_enabled": 0,
                                    "main_url": "rtsp://10.0.0.8:8554/stream0", "width": 640, "height": 360})
    runtime.hubs.input_overrides[cam] = (str(av_source), ["-re", "-stream_loop", "-1"])
    return cam


def login(c):
    r = c.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PW, "device": "pytest"})
    return r.json()["token"]


def read_until_shown(ws, timeout=20):
    """Lê mensagens até o primeiro quadro exibível; devolve (msgs, segundos)."""
    t0 = time.time()
    msgs = []
    while time.time() - t0 < timeout:
        m = ws.receive()
        data = m.get("bytes")
        if data is None:
            msgs.append(json.loads(m["text"]))
            continue
        kind, flags, ts, cts = H.HEADER.unpack_from(data)
        msgs.append((kind, flags, ts, data[H.HEADER.size:]))
        if kind == H.VIDEO and not flags & H.FLAG_CATCHUP:
            return msgs, time.time() - t0
    raise AssertionError("nenhum quadro recebido")


@needs_ffmpeg
def test_websocket_live_stream_and_instant_second_viewer(runtime, av_source):
    cam = setup_cam(runtime, av_source)
    app = create_app(runtime)
    try:
        with TestClient(app, client=("100.71.0.5", 50000)) as c:
            token = login(c)
            # Sem credencial: recusado.
            with pytest.raises(WebSocketDisconnect) as e:
                with c.websocket_connect(f"/api/v1/live/{cam}/ws") as ws:
                    ws.receive()
            assert e.value.code == 4401

            hdr = {"Authorization": f"Bearer {token}"}
            with c.websocket_connect(f"/api/v1/live/{cam}/ws", headers=hdr) as ws:
                msgs, _ = read_until_shown(ws)
            info = msgs[0]
            assert info["type"] == "info" and info["camera_id"] == cam and info["quality"] == "main"
            kinds = [m[0] for m in msgs[1:]]
            assert kinds[0] == H.VIDEO_CONFIG
            vc = next(m for m in msgs[1:] if m[0] == H.VIDEO_CONFIG)
            assert vc[3][0] == H.CODEC_H264 and vc[3][1] == 1  # avcC versão 1
            first_frame = next(m for m in msgs[1:] if m[0] == H.VIDEO)
            assert first_frame[1] & H.FLAG_KEY  # começa sempre num keyframe
            # NALs com prefixo de 4 bytes, como no MP4 (o iPhone decodifica direto).
            n = int.from_bytes(first_frame[3][:4], "big")
            assert 0 < n <= len(first_frame[3]) - 4

            # Segundo espectador: hub já conectado -> GOP guardado e imagem imediata.
            time.sleep(1.5)
            with c.websocket_connect(f"/api/v1/live/{cam}/ws", headers=hdr) as ws:
                msgs, secs = read_until_shown(ws)
                assert secs < 0.5
                frames = [m for m in msgs if isinstance(m, tuple) and m[0] == H.VIDEO]
                assert frames[0][1] & H.FLAG_KEY
                assert all(f[1] & H.FLAG_CATCHUP for f in frames[:-1])
                # Áudio G.711 µ-law 8 kHz anunciado e enviado.
                got_audio = any(isinstance(m, tuple) and m[0] == H.AUDIO_CONFIG for m in msgs)
                for _ in range(60):
                    m = ws.receive()
                    if m.get("bytes") and m["bytes"][0] == H.AUDIO_CONFIG:
                        got_audio = True
                    if got_audio:
                        break
                assert got_audio

            # Miniatura sai do keyframe do hub, sem ler gravações.
            r = c.get(f"/api/v1/cameras/{cam}/snapshot.jpg", headers=hdr)
            assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
            st = c.get("/api/v1/system/status", headers=hdr).json()
            assert st["live_hubs"][0]["state"] == "live"
    finally:
        runtime.hubs.stop()


@needs_ffmpeg
def test_hub_reconnects_and_tells_clients(runtime, av_source):
    cam = setup_cam(runtime, av_source)
    hub = runtime.hubs.get(runtime.db.get_camera(cam))
    try:
        deadline = time.time() + 20
        while hub.state != "live" and time.time() < deadline:
            time.sleep(0.2)
        assert hub.state == "live"

        async def watch():
            sub = H.Subscriber(asyncio.get_running_loop())
            hub.subscribe(sub)
            hub._kill()  # FFmpeg cai (câmera reiniciou, rede oscilou...)
            seen_reset = seen_key_after = False
            t0 = time.time()
            while time.time() - t0 < 20 and not seen_key_after:
                msg = await asyncio.wait_for(sub.next(), 20)
                if isinstance(msg, str) and json.loads(msg)["type"] == "reset":
                    seen_reset = True
                elif seen_reset and isinstance(msg, bytes) and msg[0] == H.VIDEO and msg[1] & H.FLAG_KEY:
                    seen_key_after = True
            hub.unsubscribe(sub)
            return seen_reset, seen_key_after

        assert asyncio.run(watch()) == (True, True)
        assert hub.restarts >= 1
    finally:
        runtime.hubs.stop()


def test_slow_client_skips_to_next_keyframe():
    async def run():
        sub = H.Subscriber(asyncio.get_running_loop())
        frame = lambda key: H.pack(H.VIDEO, H.FLAG_KEY if key else 0, 0, 0, b"x" * 50_000)
        sub.push("cfg")
        for i in range(30):  # cliente parado: a fila passa do limite
            sub.push(frame(i == 0), frame=True, key=i == 0)
        await asyncio.sleep(0.05)
        queued = len(sub._items)
        sub.push(frame(False), frame=True)       # ainda sem keyframe: descartado
        sub.push(frame(True), frame=True, key=True)  # recomeça aqui
        await asyncio.sleep(0.05)
        assert await sub.next() == "cfg"  # configuração nunca é descartada
        rest = []
        while sub._items:
            rest.append(await sub.next())
        return queued, rest, sub.dropped

    queued, rest, dropped = asyncio.run(run())
    assert queued <= 10 and dropped > 0
    assert rest[-1][1] & H.FLAG_KEY


def test_annexb_from_avcc():
    sps, pps = b"\x67\x64\x00\x1f\xac", b"\x68\xeb\xe3"
    avcc = bytes([1, 0x64, 0, 0x1F, 0xFF, 0xE1]) + struct.pack(">H", len(sps)) + sps + bytes([1]) + struct.pack(">H", len(pps)) + pps
    size, nals = H.config_nals(H.CODEC_H264, avcc)
    assert size == 4 and nals == [sps, pps]
    frame = struct.pack(">I", 3) + b"\x65\x88\x84" + struct.pack(">I", 2) + b"\x06\x05"
    assert H.to_annexb(4, frame) == b"\x00\x00\x00\x01\x65\x88\x84\x00\x00\x00\x01\x06\x05"


# ------------------------------------------------------------------ cópia local
def _remote_segment(runtime, cam, name, start, payload=b"\x47" + b"\x00" * 187):
    rel = f"Garagem/2026-01-01/10/{name}"
    p = runtime.cfg.storage_root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)
    os.utime(p, (start, start))
    sid = runtime.db.insert_segment({
        "camera_id": cam, "run_id": "r1", "location": "remote", "path": rel, "start_ts": start,
        "end_ts": start + 60, "duration": 60, "size": len(payload), "codec": "h264", "width": 640, "height": 360,
        "parts": [[0, len(payload), 60]],
    })
    return runtime.db.get_segment(sid)


def test_local_copy_serves_playback_and_follows_retention(runtime, monkeypatch):
    runtime.storage.check_health()
    runtime.auth.create_user("admin", ADMIN_PW, "admin")
    cam = runtime.db.insert_camera({"name": "Garagem", "host": "10.0.0.8", "folder": "Garagem", "record_enabled": 0})
    now = time.time()
    segs = [_remote_segment(runtime, cam, f"r1_{i}.ts", now - 3600 + i * 60) for i in range(4)]
    mirror = runtime.storage.mirror
    # Gravações já no disco do PC: lidas direto, sem cópia duplicada.
    assert mirror.path_for(segs[0]) is None and runtime.storage.resolve(segs[0]) == runtime.storage.source_path(segs[0])
    monkeypatch.setattr(mirror, "storage_remote", lambda: True)  # a partir daqui, como se fosse o roteador
    assert mirror.lookup(segs[0]) is None

    with TestClient(create_app(runtime), client=("127.0.0.1", 50000)) as c:
        h = {"Authorization": f"Bearer {login(c)}"}
        info = c.get(f"/api/v1/cameras/{cam}/playback", params={"start": now - 4000, "end": now}, headers=h).json()
        r = c.get(f"/api/v1/media/{segs[0]['id']}.ts", params={"mt": info["media_token"]}, headers={"Range": "bytes=0-99"})
        assert r.status_code == 206 and r.content[0] == 0x47
    # O primeiro pedido trouxe o arquivo para o PC; os próximos minutos foram para a fila.
    local = mirror.lookup(segs[0])
    assert local is not None and local.is_relative_to(runtime.cfg.mirror_dir)
    assert runtime.storage.resolve(segs[0]) == local
    queued = list(mirror._queue)
    assert queued[:3] == [s["id"] for s in segs[1:]]
    while (s := mirror._next()) is not None:
        mirror._fetch(s)
    assert all(mirror.lookup(s) for s in segs)

    # Retenção apaga o original e a cópia.
    runtime.cfg.update({"retention_days": 1})
    runtime.db.execute("UPDATE segments SET start_ts=start_ts-5*86400, end_ts=end_ts-5*86400 WHERE id=?", (segs[0]["id"],))
    runtime.storage.run_retention()
    assert not runtime.storage.source_path(segs[0]).exists() and not local.exists()

    # Limite de tamanho: as cópias mais antigas saem primeiro; o pendrive não é tocado.
    runtime.cfg.update({"local_cache_gb": 400 / 1024 ** 3})
    res = mirror.trim()
    assert res["bytes"] <= 400 and mirror.lookup(segs[3]) and not mirror.lookup(segs[1])
    assert all(runtime.storage.source_path(s).exists() for s in segs[1:])


def test_agent_dvr_files_copied_in_background_never_touched(runtime, monkeypatch):
    runtime.storage.check_health()
    cam = runtime.db.insert_camera({"name": "Garagem", "host": "10.0.0.8", "folder": "Garagem", "record_enabled": 0})
    src = runtime.cfg.agentdvr_root / "192.168.1.1" / "Garagem" / "7_01-01-2026_10-00-00_000.mkv"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 1000)
    before = src.stat()
    sid = runtime.db.insert_segment({
        "camera_id": cam, "run_id": "agentdvr", "location": "agentdvr", "path": "192.168.1.1/Garagem/7_01-01-2026_10-00-00_000.mkv",
        "container": "mkv", "start_ts": time.time() - 900, "end_ts": time.time(), "duration": 900, "size": 1004,
        "codec": "h264", "width": 640, "height": 360, "parts": [[0, 0, 900]],
    })
    seg = runtime.db.get_segment(sid)
    mirror = runtime.storage.mirror
    # Agent DVR gravando no disco do PC: lê direto, sem cópia duplicada.
    assert mirror.path_for(seg) is None and runtime.storage.resolve(seg) == src
    monkeypatch.setattr(mirror, "agentdvr_remote", lambda: True)  # pasta no roteador (\\192.168.1.1\...)
    assert mirror.ensure(seg) is None  # arquivo grande: não segura a requisição
    assert list(mirror._queue) == [sid]
    mirror._fetch(mirror._next())
    local = mirror.lookup(seg)
    assert local is not None and local.read_bytes() == src.read_bytes()
    assert runtime.storage.resolve(seg) == local
    assert src.stat().st_mtime == before.st_mtime and src.stat().st_size == before.st_size
    # Retenção nunca apaga o original do Agent DVR.
    assert runtime.storage._delete_segment(seg) is False and src.exists()


def test_settings_migration_moves_old_part_default(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps({"hls_part_seconds": 10.0, "retention_days": 15}), encoding="utf-8")
    cfg = Config(d)
    assert cfg.settings.hls_part_seconds == 4.0 and cfg.settings.retention_days == 15
    assert cfg.settings.schema_version == 2 and cfg.settings.live_always_on
    saved = json.loads((d / "settings.json").read_text(encoding="utf-8"))
    assert saved["schema_version"] == 2
    # Valor escolhido pelo usuário é mantido.
    (d / "settings.json").write_text(json.dumps({"hls_part_seconds": 6.0}), encoding="utf-8")
    assert Config(d).settings.hls_part_seconds == 6.0
