"""Supervisiona um ``CameraRecorder`` por câmera e indexa os segmentos concluídos."""
from __future__ import annotations

import logging
import os
import queue
import threading
import time
from pathlib import Path

from ..config import Config
from ..database import Database
from ..cameras.urls import build_stream_url
from .indexer import probe_segment, file_mtime
from .recorder import CameraRecorder, CompletedSegment, RecorderSpec

log = logging.getLogger("recordings.supervisor")


class RecordingSupervisor:
    def __init__(self, cfg: Config, db: Database, events):
        self.cfg = cfg
        self.db = db
        self.events = events
        self.recorders: dict[int, CameraRecorder] = {}
        self._specs: dict[int, tuple] = {}
        self._lock = threading.RLock()
        self._queue: "queue.Queue[CompletedSegment | None]" = queue.Queue()
        self._indexer = threading.Thread(target=self._index_loop, name="indexer", daemon=True)
        self._stopping = False
        # Para testes: substitui a URL de entrada (ex.: arquivo local) por câmera.
        self.input_overrides: dict[int, tuple[str, list[str]]] = {}
        # Restringe a gravação a algumas câmeras (usado pelo comando record-test).
        self.only_cameras: set[int] | None = None

    def start(self) -> None:
        self._indexer.start()
        self.reconcile_buffer()
        self.sync()

    def stop(self) -> None:
        self._stopping = True
        with self._lock:
            recs = list(self.recorders.values())
        threads = [threading.Thread(target=r.stop) for r in recs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self._queue.put(None)
        self._indexer.join(30)

    # ---------------------------------------------------------- reconcile
    def _spec_for(self, cam: dict) -> RecorderSpec | None:
        if cam["id"] in self.input_overrides:
            url, extra = self.input_overrides[cam["id"]]
        else:
            url = build_stream_url(cam, cam.get("record_stream") or "main")
            extra = []
        if not url:
            return None
        return RecorderSpec(
            camera_id=cam["id"], name=cam["name"], folder=cam["folder"], input_url=url,
            codec=(cam.get("codec") or "h264") if cam.get("record_stream") != "sub" else (cam.get("sub_codec") or cam.get("codec") or "h264"),
            audio_mode=cam.get("audio_mode") or "none", input_args=extra,
        )

    def sync(self) -> None:
        """Aplica a configuração atual sem reiniciar o servidor (só mexe no que mudou)."""
        if self._stopping:
            return
        s = self.cfg.settings
        wanted: dict[int, RecorderSpec] = {}
        for cam in self.db.list_cameras():
            if self.only_cameras is not None and cam["id"] not in self.only_cameras:
                continue
            if cam["record_enabled"]:
                spec = self._spec_for(cam)
                if spec:
                    wanted[cam["id"]] = spec
        with self._lock:
            for cam_id in list(self.recorders):
                if cam_id not in wanted:
                    self.recorders.pop(cam_id).stop()
                    self._specs.pop(cam_id, None)
            for cam_id, spec in wanted.items():
                key = (spec.input_url, spec.codec, spec.audio_mode, spec.folder, s.segment_seconds, tuple(spec.input_args))
                rec = self.recorders.get(cam_id)
                if rec and self._specs.get(cam_id) == key:
                    rec.spec.name = spec.name
                    continue
                if rec:
                    rec.stop()
                rec = CameraRecorder(
                    spec, self.cfg.ffmpeg, self.cfg.buffer_dir, s.segment_seconds, s.stall_timeout_seconds,
                    on_segment=self._queue.put,
                    on_event=lambda lvl, kind, msg, cid=cam_id: self.events.emit(lvl, kind, msg, cid),
                )
                self.recorders[cam_id] = rec
                self._specs[cam_id] = key
                rec.start()

    def restart_camera(self, cam_id: int) -> bool:
        with self._lock:
            rec = self.recorders.get(cam_id)
        if not rec:
            return False
        rec.restart()
        return True

    def status(self) -> dict[int, dict]:
        with self._lock:
            return {cid: r.status() for cid, r in self.recorders.items()}

    def active_files(self) -> set[Path]:
        """Arquivos possivelmente em escrita (não devem ser movidos/indexados ainda)."""
        out = set()
        with self._lock:
            for r in self.recorders.values():
                cur = r._current_file()
                if cur:
                    out.add(cur)
        return out

    # ------------------------------------------------------------- index
    def _index_loop(self) -> None:
        while True:
            item = self._queue.get()
            if item is None:
                return
            try:
                self.index_completed(item)
            except Exception:
                log.exception("falha ao indexar segmento")

    def index_completed(self, seg: CompletedSegment) -> int | None:
        s = self.cfg.settings
        res = probe_segment(self.cfg.ffprobe, seg.path, s.hls_part_seconds)
        rel = f"{seg.path.parent.name}/{seg.path.name}"
        if not res.ok:
            if res.size < 4096 or (res.error in ("duração muito curta", "poucos quadros") and res.size < 1_000_000):
                # Segmento vazio ou fragmento final de poucos quadros (ex.: parada do serviço).
                try:
                    seg.path.unlink()
                except OSError:
                    pass
                return None
            self.events.emit("warning", "segment", f"segmento corrompido/incompleto: {seg.path.name} ({res.error})", seg.camera_id)
            status = "corrupt"
            duration = seg.end_ts - seg.start_ts
        else:
            status = "ok"
            duration = res.duration
        # O horário de fim vem da âncora do relógio; a duração real vem do ffprobe.
        end_ts = seg.end_ts
        start_ts = end_ts - duration if status != "corrupt" else seg.start_ts
        if abs(start_ts - seg.start_ts) < 1.5:
            start_ts = seg.start_ts
        seg_id = self.db.insert_segment({
            "camera_id": seg.camera_id, "run_id": seg.run_id, "location": "local", "path": rel,
            "container": res.container, "start_ts": start_ts, "end_ts": end_ts, "duration": duration,
            "size": res.size, "codec": res.codec, "width": res.width, "height": res.height,
            "parts": res.parts, "init_len": res.init_len, "status": status,
        })
        self._autodetect_codec(seg.camera_id, res.codec)
        return seg_id

    def _autodetect_codec(self, cam_id: int, codec: str) -> None:
        if not codec:
            return
        cam = self.db.get_camera(cam_id)
        if not cam:
            return
        field = "sub_codec" if cam.get("record_stream") == "sub" else "codec"
        if cam.get(field) != codec:
            log.info("codec detectado", extra={"ctx": {"camera": cam_id, "codec": codec}})
            self.db.update_camera(cam_id, {field: codec})
            hevc_now = codec in ("hevc", "h265")
            hevc_before = (cam.get(field) or "h264") in ("hevc", "h265")
            if hevc_now != hevc_before:
                # Contêiner precisa mudar (TS <-> fMP4): reaplica a configuração.
                threading.Thread(target=self.sync, daemon=True).start()

    def reconcile_buffer(self) -> int:
        """Indexa arquivos órfãos no buffer local (ex.: queda de energia no meio de um segmento)."""
        root = self.cfg.buffer_dir
        known = self.db.known_paths("local")
        cams = {c["folder"]: c for c in self.db.list_cameras(include_deleted=True)}
        active = self.active_files()
        count = 0
        if not root.is_dir():
            return 0
        for folder in os.listdir(root):
            cam = cams.get(folder)
            d = root / folder
            if not cam or not d.is_dir():
                continue
            for name in os.listdir(d):
                p = d / name
                rel = f"{folder}/{name}"
                if rel in known or p in active or p.suffix.lower() not in (".ts", ".mp4"):
                    continue
                if self.db.one("SELECT 1 AS x FROM segments WHERE location='remote' AND path LIKE ?", (f"{folder}/%/{name}",)):
                    # Já enviado ao pendrive; a cópia local ficou presa (arquivo estava aberto).
                    p.unlink(missing_ok=True)
                    continue
                if time.time() - file_mtime(p) < self.cfg.settings.stall_timeout_seconds + 5:
                    continue
                res = probe_segment(self.cfg.ffprobe, p, self.cfg.settings.hls_part_seconds)
                if not res.ok:
                    if res.size < 4096:
                        p.unlink(missing_ok=True)
                        continue
                    status, duration = "corrupt", 0.0
                else:
                    status, duration = "partial", res.duration
                end = file_mtime(p)
                self.db.insert_segment({
                    "camera_id": cam["id"], "run_id": name.split("_", 1)[0], "location": "local", "path": rel,
                    "container": res.container, "start_ts": end - duration, "end_ts": end, "duration": duration,
                    "size": res.size, "codec": res.codec, "width": res.width, "height": res.height,
                    "parts": res.parts, "init_len": res.init_len, "status": status,
                })
                count += 1
        if count:
            self.events.emit("info", "reconcile", f"{count} segmento(s) órfão(s) recuperado(s) do buffer local")
        return count
