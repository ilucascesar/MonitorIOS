"""Ao vivo de baixa latência: conexão RTSP permanente por câmera, repassada ao app por WebSocket.

O FFmpeg converte RTSP em FLV sem recodificar o vídeo (o áudio vira G.711 µ-law 8 kHz, leve e
fácil de tocar no iPhone). O servidor lê as tags FLV, guarda o GOP atual (do último keyframe
em diante) e repassa cada quadro aos clientes. Quem conecta recebe na hora a configuração do
decodificador e o GOP guardado, então a imagem aparece sem esperar a câmera (2-3 s para abrir
o RTSP) nem a montagem de segmentos HLS.

Formato das mensagens binárias (cabeçalho de 10 bytes, big-endian):
    tipo u8 | flags u8 | timestamp_ms u32 | composição_ms i32 | dados
tipo: 1 config. de vídeo (codec u8 + avcC/hvcC), 2 quadro de vídeo (NALs com prefixo de
tamanho, como no MP4), 3 config. de áudio (codec u8, taxa u32, canais u8), 4 áudio.
flags: bit 0 keyframe, bit 1 "atualização" (quadro do GOP guardado: decodificar sem exibir).
Mensagens de texto são JSON: {"type": "info" | "reset" | "error", ...}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import subprocess
import sys
import threading
import time
from collections import deque
from typing import Callable

from ..logging_setup import redact

log = logging.getLogger("streaming.hub")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

VIDEO_CONFIG, VIDEO, AUDIO_CONFIG, AUDIO = 1, 2, 3, 4
FLAG_KEY, FLAG_CATCHUP = 1, 2
CODEC_H264, CODEC_HEVC = 1, 2
AUDIO_MULAW = 1
HEADER = struct.Struct(">BBIi")

STALL_SECONDS = 10.0
GOP_MAX_BYTES = 4 * 1024 * 1024


def pack(kind: int, flags: int, ts: int, cts: int, payload: bytes) -> bytes:
    return HEADER.pack(kind, flags, ts & 0xFFFFFFFF, cts) + payload


def with_flags(msg: bytes, flags: int) -> bytes:
    return msg[:1] + bytes([msg[1] | flags]) + msg[2:]


# ------------------------------------------------------------------ FLV
def _read_exact(stream, n: int) -> bytes:
    data = stream.read(n)
    if len(data) == n or not data:
        return data
    chunks = [data]
    got = len(data)
    while got < n:
        more = stream.read(n - got)
        if not more:
            break
        chunks.append(more)
        got += len(more)
    return b"".join(chunks)


def read_flv(stream):
    """Gera (tipo_tag, timestamp_ms, corpo) a partir de um fluxo FLV."""
    head = _read_exact(stream, 9)
    if len(head) < 9 or head[:3] != b"FLV":
        raise ValueError("fluxo FLV inválido")
    _read_exact(stream, int.from_bytes(head[5:9], "big") - 9 + 4)
    while True:
        th = _read_exact(stream, 11)
        if len(th) < 11:
            return
        size = int.from_bytes(th[1:4], "big")
        ts = int.from_bytes(th[4:7], "big") | (th[7] << 24)
        body = _read_exact(stream, size)
        _read_exact(stream, 4)
        if len(body) < size:
            return
        yield th[0] & 0x1F, ts, body


def parse_video(body: bytes) -> tuple[str, int, bool, int, bytes] | None:
    """Tag de vídeo FLV (clássica ou "enhanced") -> (tipo, codec, keyframe, cts, dados).

    tipo: "config" (avcC/hvcC) ou "frame" (NALs com prefixo de tamanho)."""
    if len(body) < 2:
        return None
    b0 = body[0]
    if b0 & 0x80:  # Enhanced RTMP/FLV (HEVC no FFmpeg >= 6.1)
        frame_type, packet = (b0 >> 4) & 0x07, b0 & 0x0F
        fourcc = body[1:5]
        codec = CODEC_HEVC if fourcc in (b"hvc1", b"hev1") else CODEC_H264 if fourcc == b"avc1" else 0
        if not codec:
            return None
        if packet == 0:
            return "config", codec, False, 0, body[5:]
        if packet == 1:
            return "frame", codec, frame_type == 1, int.from_bytes(body[5:8], "big", signed=True), body[8:]
        if packet == 3:
            return "frame", codec, frame_type == 1, 0, body[5:]
        return None
    frame_type, codec_id = b0 >> 4, b0 & 0x0F
    codec = {7: CODEC_H264, 12: CODEC_HEVC}.get(codec_id)
    if not codec or len(body) < 5:
        return None
    cts = int.from_bytes(body[2:5], "big", signed=True)
    if body[1] == 0:
        return "config", codec, False, 0, body[5:]
    if body[1] == 1:
        return "frame", codec, frame_type == 1, cts, body[5:]
    return None


# ------------------------------------------------------------- Annex B
def config_nals(codec: int, record: bytes) -> tuple[int, list[bytes]]:
    """(tamanho do prefixo dos NALs, [VPS/SPS/PPS]) a partir do avcC/hvcC."""
    nals: list[bytes] = []
    if codec == CODEC_H264:
        length_size = (record[4] & 0x03) + 1
        pos = 5
        for group in range(2):
            count = record[pos] & (0x1F if group == 0 else 0xFF)
            pos += 1
            for _ in range(count):
                n = int.from_bytes(record[pos:pos + 2], "big")
                nals.append(record[pos + 2:pos + 2 + n])
                pos += 2 + n
        return length_size, nals
    length_size = (record[21] & 0x03) + 1
    pos = 23
    for _ in range(record[22]):
        count = int.from_bytes(record[pos + 1:pos + 3], "big")
        pos += 3
        for _ in range(count):
            n = int.from_bytes(record[pos:pos + 2], "big")
            nals.append(record[pos + 2:pos + 2 + n])
            pos += 2 + n
    return length_size, nals


def to_annexb(length_size: int, data: bytes) -> bytes:
    out, pos = [], 0
    while pos + length_size <= len(data):
        n = int.from_bytes(data[pos:pos + length_size], "big")
        pos += length_size
        out.append(b"\x00\x00\x00\x01" + data[pos:pos + n])
        pos += n
    return b"".join(out)


# --------------------------------------------------------- assinantes
class Subscriber:
    """Fila de um cliente WebSocket. Recebe do thread do hub; consome no loop asyncio.

    Se a rede do cliente não acompanha, os quadros atrasados são descartados e o envio
    recomeça no próximo keyframe: a imagem salta para o presente em vez de acumular atraso."""

    MAX_QUEUED_BYTES = 400_000

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.loop = loop
        self._items: deque[tuple[bytes | str | None, bool]] = deque()
        self._bytes = 0
        self._event = asyncio.Event()
        self._skip_to_key = False
        self.closed = False
        self.dropped = 0

    def push(self, msg: bytes | str | None, frame: bool = False, key: bool = False) -> None:
        if self.closed:
            return
        try:
            self.loop.call_soon_threadsafe(self._push, msg, frame, key)
        except RuntimeError:
            self.closed = True

    def close(self) -> None:
        self.push(None)

    def _push(self, msg, frame: bool, key: bool) -> None:
        if frame:
            if self._skip_to_key and not key:
                return
            if self._bytes > self.MAX_QUEUED_BYTES:
                kept = deque(i for i in self._items if not i[1])
                self.dropped += len(self._items) - len(kept)
                self._items = kept
                self._bytes = sum(len(i[0]) for i in kept if i[0] is not None)
                if not key:
                    self._skip_to_key = True
                    return
            if key:
                self._skip_to_key = False
        self._items.append((msg, frame))
        if msg is not None:
            self._bytes += len(msg)
        self._event.set()

    async def next(self) -> bytes | str | None:
        while not self._items:
            self._event.clear()
            await self._event.wait()
        msg, _ = self._items.popleft()
        if msg is not None:
            self._bytes -= len(msg)
        return msg


# ----------------------------------------------------------------- hub
class LiveHub:
    def __init__(self, key: tuple[int, str], url: str, ffmpeg: str, input_args: list[str] | None = None,
                 always_on: bool = False):
        self.key = key
        self.url = url              # contém credenciais: nunca logar
        self.ffmpeg = ffmpeg
        self.input_args = input_args or []
        self.always_on = always_on
        self.state = "stopped"      # stopped | connecting | live | retrying
        self.last_error = ""
        self.started_at = 0.0
        self.last_frame_at = 0.0
        self.last_used = time.time()
        self.restarts = 0
        self.codec = 0
        self.video_config: bytes | None = None
        self.audio_config: bytes | None = None
        self.length_size = 4
        self.param_nals: list[bytes] = []
        self._gop: list[bytes] = []
        self._gop_bytes = 0
        self._subs: set[Subscriber] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
        self._stderr: deque[str] = deque(maxlen=10)

    # ---------------------------------------------------------- clientes
    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def subscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self.last_used = time.time()
            for msg in (self.video_config, self.audio_config):
                if msg:
                    sub.push(msg)
            gop = list(self._gop)
            for i, msg in enumerate(gop):
                # Quadros já passados: o app decodifica sem exibir; o último aparece de imediato.
                sub.push(with_flags(msg, FLAG_CATCHUP) if i < len(gop) - 1 else msg, frame=True, key=i == 0)
            self._subs.add(sub)

    def unsubscribe(self, sub: Subscriber) -> None:
        with self._lock:
            self._subs.discard(sub)
            self.last_used = time.time()

    def _broadcast(self, msg: bytes | str, frame: bool = False, key: bool = False) -> None:
        for sub in list(self._subs):
            if sub.closed:
                self._subs.discard(sub)
            else:
                sub.push(msg, frame, key)

    def keyframe(self) -> tuple[int, bytes] | None:
        """(timestamp, keyframe em Annex B com VPS/SPS/PPS) para gerar miniaturas."""
        with self._lock:
            if not self._gop or not self.param_nals:
                return None
            msg = self._gop[0]
            ts = HEADER.unpack_from(msg)[2]
            data = b"".join(b"\x00\x00\x00\x01" + n for n in self.param_nals) + to_annexb(self.length_size, msg[HEADER.size:])
            return ts, data

    # -------------------------------------------------------- ciclo de vida
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        if self.alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"hub-{self.key[0]}-{self.key[1]}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._kill()
        with self._lock:
            for sub in self._subs:
                sub.close()
            self._subs.clear()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(5)
        self.state = "stopped"

    def _kill(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            if sys.platform == "win32":
                # Árvore inteira: um ffmpeg.exe instalado como "shim" (Chocolatey, Scoop) roda o
                # FFmpeg real como filho, que continuaria segurando a câmera e o pipe.
                try:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW, timeout=10, check=False)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            try:
                proc.kill()
            except OSError:
                pass

    def build_command(self) -> list[str]:
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats"]
        if self.url.lower().startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
        cmd += ["-fflags", "+genpts+nobuffer", "-flags", "low_delay", *self.input_args, "-i", self.url,
                "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "pcm_mulaw", "-ar", "8000", "-ac", "1",
                "-flush_packets", "1", "-f", "flv", "-flvflags", "no_duration_filesize", "pipe:1"]
        return cmd

    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            self.state = "connecting"
            self.started_at = time.time()
            try:
                self._run_once()
            except Exception as e:  # noqa: BLE001 - o hub nunca pode morrer
                self.last_error = redact(str(e))[:200]
                log.warning("falha no ao vivo", extra={"ctx": {"camera": self.key[0], "error": self.last_error}})
            with self._lock:
                self._gop, self._gop_bytes = [], 0
            if self._stop.is_set():
                break
            self.restarts += 1
            self.state = "retrying"
            if time.time() - self.started_at > 60:
                backoff = 1.0
            self._broadcast(json.dumps({"type": "reset", "reason": self.last_error or "reconectando"}))
            self._stop.wait(backoff)
            backoff = min(backoff * 2, 30.0)
        self.state = "stopped"

    def _run_once(self) -> None:
        proc = subprocess.Popen(
            self.build_command(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW, bufsize=0,
        )
        self._proc = proc
        threading.Thread(target=self._drain, args=(proc,), daemon=True).start()
        threading.Thread(target=self._watchdog, args=(proc,), daemon=True).start()
        log.info("ao vivo conectando", extra={"ctx": {"camera": self.key[0], "quality": self.key[1], "pid": proc.pid}})
        try:
            for tag, ts, body in read_flv(proc.stdout):
                if self._stop.is_set():
                    break
                if tag == 9:
                    self._on_video(ts, body)
                elif tag == 8 and body:
                    self._on_audio(ts, body)
        finally:
            self._kill()
            proc.wait()
            self._proc = None
        if not self._stop.is_set():
            tail = " | ".join(self._stderr) or f"ffmpeg saiu (código {proc.returncode})"
            self.last_error = redact(tail)[:200]

    def _on_video(self, ts: int, body: bytes) -> None:
        parsed = parse_video(body)
        if not parsed:
            return
        kind, codec, key, cts, data = parsed
        if kind == "config":
            msg = pack(VIDEO_CONFIG, 0, ts, 0, bytes([codec]) + data)
            with self._lock:
                self.codec = codec
                self.video_config = msg
                try:
                    self.length_size, self.param_nals = config_nals(codec, data)
                except (IndexError, ValueError):
                    self.param_nals = []
                self._broadcast(msg)
            return
        if self.video_config is None or not data:
            return
        msg = pack(VIDEO, FLAG_KEY if key else 0, ts, cts, data)
        with self._lock:
            if key:
                self._gop, self._gop_bytes = [msg], len(msg)
            elif self._gop and self._gop_bytes + len(msg) <= GOP_MAX_BYTES:
                self._gop.append(msg)
                self._gop_bytes += len(msg)
            self.last_frame_at = time.time()
            if self.state != "live" and key:
                self.state = "live"
                self.last_error = ""
                log.info("ao vivo no ar", extra={"ctx": {"camera": self.key[0], "quality": self.key[1],
                                                         "ms": round((time.time() - self.started_at) * 1000)}})
            self._broadcast(msg, frame=True, key=key)

    def _on_audio(self, ts: int, body: bytes) -> None:
        if body[0] >> 4 != 8:  # só µ-law (é o que o FFmpeg foi instruído a gerar)
            return
        with self._lock:
            if self.audio_config is None:
                rate = 8000
                self.audio_config = pack(AUDIO_CONFIG, 0, ts, 0, struct.pack(">BIB", AUDIO_MULAW, rate, 1))
                self._broadcast(self.audio_config)
            self._broadcast(pack(AUDIO, 0, ts, 0, body[1:]), frame=True)

    def _drain(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            line = redact(raw.decode("utf-8", "replace").strip())
            if line:
                self._stderr.append(line[:200])

    def _watchdog(self, proc: subprocess.Popen) -> None:
        """RTSP travado não fecha o pipe: sem quadros por STALL_SECONDS, o FFmpeg é reiniciado."""
        while proc.poll() is None and not self._stop.is_set():
            time.sleep(1)
            ref = self.last_frame_at if self.last_frame_at > self.started_at else self.started_at
            if time.time() - ref > (STALL_SECONDS if self.state == "live" else 20):
                self._stderr.append("sem imagem da câmera")
                self._kill()
                return


class HubManager:
    """Um hub por (câmera, qualidade). Com ``live_always_on``, a qualidade principal de cada câmera
    fica sempre conectada; as demais abrem sob demanda e fecham quando ninguém assiste."""

    def __init__(self, cfg, db, url_for: Callable[[dict, str], str]):
        self.cfg = cfg
        self.db = db
        self.url_for = url_for
        self.hubs: dict[tuple[int, str], LiveHub] = {}
        self.input_overrides: dict[int, tuple[str, list[str]]] = {}  # testes: arquivo local no lugar do RTSP
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshots: dict[int, tuple[int, bytes]] = {}
        self._snap_lock = threading.Lock()

    def start(self) -> None:
        self.sync()
        self._thread = threading.Thread(target=self._reap_loop, name="hub-reaper", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            hubs = list(self.hubs.values())
            self.hubs.clear()
        for h in hubs:
            h.stop()

    def _source(self, cam: dict, quality: str) -> tuple[str, list[str]]:
        if cam["id"] in self.input_overrides:
            return self.input_overrides[cam["id"]]
        return self.url_for(cam, quality), []

    def resolve_quality(self, cam: dict, quality: str) -> str:
        return "sub" if quality == "sub" and cam.get("sub_url") else "main"

    def get(self, cam: dict, quality: str = "main") -> LiveHub:
        quality = self.resolve_quality(cam, quality)
        key = (cam["id"], quality)
        url, extra = self._source(cam, quality)
        if not url:
            raise ValueError("câmera sem URL RTSP configurada")
        with self._lock:
            hub = self.hubs.get(key)
            if hub and (hub.url != url or not hub.alive()):
                hub.stop()
                hub = None
            if hub is None:
                always = quality == "main" and self.cfg.settings.live_always_on
                hub = LiveHub(key, url, self.cfg.ffmpeg, extra, always_on=always)
                hub.start()
                self.hubs[key] = hub
            hub.last_used = time.time()
            return hub

    def sync(self) -> None:
        """Mantém os hubs permanentes de acordo com o cadastro (câmeras novas, removidas, URL trocada)."""
        cams = {c["id"]: c for c in self.db.list_cameras() if c["live_enabled"]}
        with self._lock:
            stale = [k for k, h in self.hubs.items() if k[0] not in cams]
            for k in stale:
                self.hubs.pop(k).stop()
        if not self.cfg.settings.live_always_on:
            return
        for cam in cams.values():
            if cam["main_url"] or cam["id"] in self.input_overrides:
                try:
                    self.get(cam, "main")
                except ValueError:
                    pass

    def stop_camera(self, cam_id: int) -> None:
        with self._lock:
            keys = [k for k in self.hubs if k[0] == cam_id]
            hubs = [self.hubs.pop(k) for k in keys]
        for h in hubs:
            h.stop()
        self._snapshots.pop(cam_id, None)

    def active(self) -> list[dict]:
        with self._lock:
            return [
                {"camera_id": k[0], "quality": k[1], "state": h.state, "viewers": h.subscribers,
                 "since": h.started_at, "last_frame_at": h.last_frame_at or None, "restarts": h.restarts,
                 "last_error": h.last_error, "always_on": h.always_on}
                for k, h in self.hubs.items()
            ]

    def snapshot(self, cam_id: int, width: int = 640) -> bytes | None:
        """JPEG do keyframe mais recente do hub (~100 ms, sem abrir conexão com a câmera)."""
        hub = self.hubs.get((cam_id, "main")) or self.hubs.get((cam_id, "sub"))
        if not hub or hub.state != "live":
            return None
        kf = hub.keyframe()
        if not kf:
            return None
        ts, annexb = kf
        cached = self._snapshots.get(cam_id)
        if cached and cached[0] == ts:
            return cached[1]
        with self._snap_lock:
            fmt = "hevc" if hub.codec == CODEC_HEVC else "h264"
            try:
                cp = subprocess.run(
                    [self.cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-f", fmt, "-i", "pipe:0", "-frames:v", "1",
                     "-vf", f"scale={width}:-2", "-q:v", "5", "-f", "image2", "-c:v", "mjpeg", "pipe:1"],
                    input=annexb, capture_output=True, timeout=10, creationflags=CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                return None
            if cp.returncode != 0 or not cp.stdout:
                return None
            self._snapshots[cam_id] = (ts, cp.stdout)
            return cp.stdout

    def _reap_loop(self) -> None:
        while not self._stop.wait(5):
            idle = self.cfg.settings.live_idle_timeout_seconds
            now = time.time()
            with self._lock:
                for key, hub in list(self.hubs.items()):
                    if not hub.always_on and hub.subscribers == 0 and now - hub.last_used > idle:
                        self.hubs.pop(key)
                        threading.Thread(target=hub.stop, daemon=True).start()
                        log.info("ao vivo encerrado (sem espectadores)", extra={"ctx": {"camera": key[0], "quality": key[1]}})
