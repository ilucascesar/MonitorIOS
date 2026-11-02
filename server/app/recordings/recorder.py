"""Gravação contínua de uma câmera com FFmpeg (um processo independente por câmera).

Fluxo:
  RTSP --(ffmpeg -c copy, muxer segment)--> buffer local/<pasta>/<run>_<data>.ts
  ffmpeg escreve no stdout (segment_list CSV) cada segmento concluído com tempos do fluxo.
  O horário real é ancorado no relógio do servidor: âncora = min(agora - fim_no_fluxo) numa
  janela recente, o que elimina a latência de notificação e acompanha o drift do relógio da
  câmera. Assim os segmentos de uma mesma execução ficam contíguos na linha do tempo.

Falhas (câmera offline, RTSP travado, crash) reiniciam o processo com backoff exponencial,
sem afetar as demais câmeras.
"""
from __future__ import annotations

import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..logging_setup import redact

log = logging.getLogger("recordings.recorder")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000 if sys.platform == "win32" else 0


@dataclass
class RecorderSpec:
    camera_id: int
    name: str
    folder: str
    input_url: str              # contém credenciais: nunca logar
    codec: str = "h264"         # define o contêiner: h264 -> ts, hevc -> fMP4
    audio_mode: str = "none"    # none | aac | copy
    input_args: list[str] = field(default_factory=list)  # usado nos testes (ex.: ["-re", "-stream_loop", "-1"])


@dataclass
class CompletedSegment:
    camera_id: int
    run_id: str
    path: Path
    start_ts: float
    end_ts: float
    stream_start: float
    stream_end: float


class CameraRecorder:
    def __init__(
        self,
        spec: RecorderSpec,
        ffmpeg: str,
        buffer_root: Path,
        segment_seconds: int,
        stall_timeout: int,
        on_segment: Callable[[CompletedSegment], None],
        on_event: Callable[[str, str, str], None] | None = None,
    ):
        self.spec = spec
        self.ffmpeg = ffmpeg
        self.dir = buffer_root / spec.folder
        self.segment_seconds = segment_seconds
        self.stall_timeout = stall_timeout
        self.on_segment = on_segment
        self.on_event = on_event or (lambda lvl, kind, msg: None)

        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen | None = None
        self._proc_lock = threading.Lock()
        self.stderr_tail: deque[str] = deque(maxlen=40)

        self.state = "stopped"          # stopped | starting | recording | retrying
        self.run_id = ""
        self.started_at = 0.0
        self.last_segment_at = 0.0
        self.last_data_at = 0.0
        self.restarts = 0
        self.consecutive_failures = 0
        self.last_error = ""
        self.online = False
        self._anchors: deque[float] = deque(maxlen=10)
        self._last_end_wall: float | None = None

    # ----------------------------------------------------------- lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"rec-{self.spec.camera_id}", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15) -> None:
        self._stop.set()
        self._terminate_proc(graceful=True)
        if self._thread:
            self._thread.join(timeout)
        self.state = "stopped"
        self.online = False

    def restart(self) -> None:
        self._restart.set()
        self._terminate_proc(graceful=True)

    def status(self) -> dict:
        return {
            "state": self.state,
            "online": self.online,
            "run_id": self.run_id,
            "started_at": self.started_at or None,
            "last_segment_at": self.last_segment_at or None,
            "last_data_at": self.last_data_at or None,
            "restarts": self.restarts,
            "last_error": self.last_error,
            "pid": self._proc.pid if self._proc and self._proc.poll() is None else None,
        }

    # --------------------------------------------------------------- core
    def build_command(self) -> tuple[list[str], str]:
        hevc = self.spec.codec.lower() in ("hevc", "h265")
        ext = "mp4" if hevc else "ts"
        pattern = str(self.dir / f"{self.run_id}_%Y%m%d-%H%M%S.{ext}")
        cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "warning", "-nostats"]
        if self.spec.input_url.lower().startswith("rtsp"):
            cmd += ["-rtsp_transport", "tcp", "-timeout", "15000000"]
        cmd += ["-fflags", "+genpts", *self.spec.input_args, "-i", self.spec.input_url, "-map", "0:v:0"]
        if self.spec.audio_mode in ("aac", "copy"):
            cmd += ["-map", "0:a:0?"]
        cmd += ["-c:v", "copy"]
        if self.spec.audio_mode == "aac":
            cmd += ["-c:a", "aac", "-b:a", "64k", "-ar", "16000"]
        elif self.spec.audio_mode == "copy":
            cmd += ["-c:a", "copy"]
        else:
            cmd += ["-an"]
        cmd += ["-dn", "-sn"]
        if hevc:
            cmd += ["-tag:v", "hvc1"]
        cmd += [
            "-f", "segment",
            "-segment_time", str(self.segment_seconds),
            "-segment_atclocktime", "1",
            "-segment_format", "mp4" if hevc else "mpegts",
        ]
        if hevc:
            cmd += ["-segment_format_options", "movflags=+frag_keyframe+empty_moov+default_base_moof+skip_trailer"]
        cmd += ["-strftime", "1", "-segment_list", "pipe:1", "-segment_list_type", "csv", pattern]
        return cmd, ext

    def _loop(self) -> None:
        backoff = 2.0
        while not self._stop.is_set():
            self._restart.clear()
            self.dir.mkdir(parents=True, exist_ok=True)
            self.run_id = secrets.token_hex(3)
            self._anchors.clear()
            self._last_end_wall = None
            cmd, _ = self.build_command()
            self.state = "starting"
            self.started_at = time.time()
            self.last_data_at = time.time()
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS,
                )
            except OSError as e:
                self._fail(f"não foi possível iniciar o ffmpeg: {e}")
                self._sleep(30)
                continue
            with self._proc_lock:
                self._proc = proc
            log.info("gravação iniciada", extra={"ctx": {"camera": self.spec.camera_id, "run": self.run_id, "pid": proc.pid}})
            t_out = threading.Thread(target=self._read_stdout, args=(proc,), daemon=True)
            t_err = threading.Thread(target=self._read_stderr, args=(proc,), daemon=True)
            t_out.start()
            t_err.start()
            stalled = self._watch(proc)
            rc = proc.wait()
            t_out.join(5)
            t_err.join(5)
            with self._proc_lock:
                self._proc = None
            self.online = False
            if self._stop.is_set():
                break
            if self._restart.is_set():
                log.info("gravação reiniciada a pedido", extra={"ctx": {"camera": self.spec.camera_id}})
                backoff = 2.0
                continue
            tail = redact(" | ".join(list(self.stderr_tail)[-3:]))
            reason = "fluxo travado (sem dados)" if stalled else f"ffmpeg saiu (código {rc})"
            self._fail(f"{reason}: {tail}"[:500])
            ran_for = time.time() - self.started_at
            if ran_for > 120:
                backoff = 2.0
            self.state = "retrying"
            self._sleep(backoff)
            backoff = min(backoff * 2, 60.0)
        self.state = "stopped"

    def _fail(self, msg: str) -> None:
        self.restarts += 1
        self.consecutive_failures += 1
        self.last_error = msg
        log.warning("falha na gravação", extra={"ctx": {"camera": self.spec.camera_id, "error": msg}})
        if self.consecutive_failures in (1, 5) or self.consecutive_failures % 30 == 0:
            self.on_event("warning", "recorder", f"{self.spec.name}: {msg}")

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(seconds)

    def _watch(self, proc: subprocess.Popen) -> bool:
        """Monitora crescimento do arquivo atual; mata o ffmpeg se o fluxo travar."""
        last_size = -1
        while proc.poll() is None:
            if self._stop.wait(2) or self._restart.is_set():
                return False
            current = self._current_file()
            if current is not None:
                try:
                    size = current.stat().st_size
                except OSError:
                    size = last_size
                if size != last_size:
                    last_size = size
                    self.last_data_at = time.time()
                    if self.state != "recording":
                        self.state = "recording"
                    if not self.online:
                        self.online = True
                        if self.consecutive_failures:
                            self.on_event("info", "recorder", f"{self.spec.name}: gravação restabelecida")
                        self.consecutive_failures = 0
            if time.time() - self.last_data_at > self.stall_timeout:
                log.warning("fluxo sem dados; reiniciando ffmpeg", extra={"ctx": {"camera": self.spec.camera_id}})
                self._terminate_proc(graceful=False)
                return True
        return False

    def _current_file(self) -> Path | None:
        newest, newest_m = None, 0.0
        try:
            with os.scandir(self.dir) as it:
                for e in it:
                    if e.is_file() and e.name.startswith(self.run_id + "_"):
                        m = e.stat().st_mtime
                        if m >= newest_m:
                            newest, newest_m = Path(e.path), m
        except OSError:
            return None
        return newest

    def _read_stdout(self, proc: subprocess.Popen) -> None:
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                name, s, e = line.rsplit(",", 2)
                s_f, e_f = float(s), float(e)
            except ValueError:
                continue
            now = time.time()
            self._anchors.append(now - e_f)
            anchor = min(self._anchors)
            start_wall, end_wall = anchor + s_f, anchor + e_f
            # Continuidade dentro da mesma execução: não deixa micro-lacunas/sobreposições.
            if self._last_end_wall is not None and abs(start_wall - self._last_end_wall) < 1.5:
                start_wall = self._last_end_wall
            self._last_end_wall = end_wall
            self.last_segment_at = now
            path = self.dir / Path(name.strip('"')).name
            seg = CompletedSegment(self.spec.camera_id, self.run_id, path, start_wall, end_wall, s_f, e_f)
            try:
                self.on_segment(seg)
            except Exception:
                log.exception("erro ao indexar segmento", extra={"ctx": {"camera": self.spec.camera_id}})

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        assert proc.stderr is not None
        for raw in iter(proc.stderr.readline, b""):
            line = redact(raw.decode("utf-8", "replace").strip())
            if line:
                self.stderr_tail.append(line[:300])
                log.debug("ffmpeg", extra={"ctx": {"camera": self.spec.camera_id, "line": line[:300]}})

    def _terminate_proc(self, graceful: bool) -> None:
        with self._proc_lock:
            proc = self._proc
        if not proc or proc.poll() is not None:
            return
        if graceful and proc.stdin:
            try:
                proc.stdin.write(b"q")
                proc.stdin.flush()
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(8)
                return
            except subprocess.TimeoutExpired:
                pass
        try:
            proc.kill()
        except OSError:
            pass
