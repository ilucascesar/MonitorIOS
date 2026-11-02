"""Ao vivo: RTSP -> HLS sob demanda, um único processo FFmpeg por (câmera, qualidade).

O processo é compartilhado por todos os clientes e encerrado após ``live_idle_timeout``
sem requisições. Sem transcodificação (``-c copy``); H.265 sai em fMP4 (exigência do HLS).
"""
from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

from ..config import Config
from ..logging_setup import redact

log = logging.getLogger("streaming.live")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000 if sys.platform == "win32" else 0
SAFE_NAME = re.compile(r"^(index\.m3u8|init\.mp4|seg_\d{1,8}\.(ts|m4s))$")


class LiveStream:
    def __init__(self, key: tuple[int, str], url: str, codec: str, out_dir: Path, ffmpeg: str, hls_time: int, input_args=None):
        self.key = key
        self.url = url
        self.codec = codec
        self.dir = out_dir
        self.ffmpeg = ffmpeg
        self.hls_time = hls_time
        self.input_args = input_args or []
        self.proc: subprocess.Popen | None = None
        self.last_access = time.time()
        self.started_at = 0.0
        self.stderr_tail: deque[str] = deque(maxlen=20)

    @property
    def playlist(self) -> Path:
        return self.dir / "index.m3u8"

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def start(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)
        self.dir.mkdir(parents=True, exist_ok=True)
        hevc = self.codec.lower() in ("hevc", "h265")
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats"]
        if self.url.lower().startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp", "-timeout", "10000000"]
        cmd += ["-fflags", "+genpts+nobuffer", *self.input_args, "-i", self.url, "-map", "0:v:0", "-c:v", "copy", "-an"]
        if hevc:
            cmd += ["-tag:v", "hvc1"]
        cmd += [
            "-f", "hls", "-hls_time", str(self.hls_time), "-hls_list_size", "6",
            "-hls_flags", "delete_segments+independent_segments+program_date_time+omit_endlist+temp_file",
            "-hls_segment_type", "fmp4" if hevc else "mpegts",
            "-hls_segment_filename", str(self.dir / ("seg_%05d.m4s" if hevc else "seg_%05d.ts")),
        ]
        if hevc:
            cmd += ["-hls_fmp4_init_filename", "init.mp4"]
        cmd.append(str(self.playlist))
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS,
        )
        self.started_at = time.time()
        threading.Thread(target=self._drain, daemon=True).start()
        log.info("ao vivo iniciado", extra={"ctx": {"camera": self.key[0], "quality": self.key[1], "pid": self.proc.pid}})

    def _drain(self) -> None:
        proc = self.proc
        if not proc or not proc.stderr:
            return
        for raw in iter(proc.stderr.readline, b""):
            line = redact(raw.decode("utf-8", "replace").strip())
            if line:
                self.stderr_tail.append(line[:300])

    def stop(self) -> None:
        proc = self.proc
        if proc and proc.poll() is None:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.wait(5)
            except Exception:
                proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)


class LiveManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.streams: dict[tuple[int, str], LiveStream] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reaper = threading.Thread(target=self._reap_loop, name="live-reaper", daemon=True)
        self.input_overrides: dict[int, tuple[str, list[str]]] = {}

    def start(self) -> None:
        shutil.rmtree(self.cfg.live_dir, ignore_errors=True)
        self.cfg.live_dir.mkdir(parents=True, exist_ok=True)
        self._reaper.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            for s in self.streams.values():
                s.stop()
            self.streams.clear()

    def ensure(self, cam_id: int, quality: str, url: str, codec: str) -> LiveStream:
        key = (cam_id, quality)
        with self._lock:
            st = self.streams.get(key)
            if st and st.alive():
                st.last_access = time.time()
                return st
            if st:
                st.stop()
            alive = [s for s in self.streams.values() if s.alive()]
            if len(alive) >= self.cfg.settings.live_max_processes:
                oldest = min(alive, key=lambda s: s.last_access)
                oldest.stop()
                self.streams.pop(oldest.key, None)
            extra: list[str] = []
            if cam_id in self.input_overrides:
                url, extra = self.input_overrides[cam_id]
            st = LiveStream(key, url, codec, self.cfg.live_dir / f"{cam_id}_{quality}", self.cfg.ffmpeg,
                            self.cfg.settings.live_hls_time, extra)
            st.start()
            self.streams[key] = st
            return st

    def get(self, cam_id: int, quality: str) -> LiveStream | None:
        st = self.streams.get((cam_id, quality))
        if st:
            st.last_access = time.time()
        return st

    def wait_ready(self, st: LiveStream, timeout: float = 20) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if st.playlist.exists():
                try:
                    if "#EXTINF" in st.playlist.read_text(encoding="utf-8", errors="replace"):
                        return True
                except OSError:
                    pass
            if not st.alive():
                return False
            time.sleep(0.25)
        return False

    def active(self) -> list[dict]:
        with self._lock:
            return [
                {"camera_id": k[0], "quality": k[1], "since": s.started_at, "last_access": s.last_access}
                for k, s in self.streams.items() if s.alive()
            ]

    def stop_camera(self, cam_id: int) -> None:
        with self._lock:
            for key in [k for k in self.streams if k[0] == cam_id]:
                self.streams.pop(key).stop()

    def _reap_loop(self) -> None:
        while not self._stop.wait(5):
            idle = self.cfg.settings.live_idle_timeout_seconds
            now = time.time()
            with self._lock:
                for key, st in list(self.streams.items()):
                    if now - st.last_access > idle or (not st.alive() and now - st.started_at > 30):
                        st.stop()
                        self.streams.pop(key, None)
                        log.info("ao vivo encerrado", extra={"ctx": {"camera": key[0], "quality": key[1]}})
