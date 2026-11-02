"""Monta e controla todos os serviços do servidor (usado pela API, pela CLI e pelo serviço)."""
from __future__ import annotations

import logging
import time
from pathlib import Path

from . import __version__
from .cameras.service import CameraService
from .config import Config
from .database import Database
from .events import EventBus
from .recordings.export import ExportManager
from .recordings.supervisor import RecordingSupervisor
from .security.auth import AuthService
from .storage.manager import StorageManager
from .cameras.urls import build_stream_url
from .streaming.hub import HubManager
from .streaming.live import LiveManager

log = logging.getLogger("runtime")


class Runtime:
    def __init__(self, data_dir: Path | None = None, start_workers: bool = True):
        self.cfg = Config(data_dir)
        self.db = Database(self.cfg.db_path)
        self.events = EventBus(self.db)
        self.auth = AuthService(self.db, self.cfg.settings.session_ttl_hours)
        self.cameras = CameraService(self.cfg, self.db, self.events)
        self.supervisor = RecordingSupervisor(self.cfg, self.db, self.events)
        self.storage = StorageManager(self.cfg, self.db, self.events, self.supervisor.active_files)
        self.live = LiveManager(self.cfg)
        self.hubs = HubManager(self.cfg, self.db, build_stream_url)
        self.exports = ExportManager(self.cfg, self.db, self.storage, self.events)
        self.started_at = time.time()
        self.version = __version__
        self._start_workers = start_workers
        self._running = False

    def start(self) -> None:
        if self._running or not self._start_workers:
            return
        self._running = True
        log.info("servidor iniciando", extra={"ctx": {"version": self.version, "data_dir": str(self.cfg.data_dir)}})
        self.events.emit("info", "server", f"servidor iniciado (v{self.version})")
        self.storage.check_health()
        self.storage.start()
        self.supervisor.start()
        self.live.start()
        self.hubs.start()
        self.exports.start()
        self.cameras.start_health()

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        log.info("servidor encerrando")
        self.cameras.stop()
        self.hubs.stop()
        self.live.stop()
        self.supervisor.stop()   # fecha os segmentos em andamento com 'q' (arquivos válidos)
        self.exports.stop()
        self.storage.stop()
        self.events.emit("info", "server", "servidor encerrado")

    def apply_camera_changes(self, cam_id: int | None = None) -> None:
        self.supervisor.sync()
        # Câmera nova pode já ter vídeos do MonitorIOS no pendrive (reinstalação): indexa-os.
        self.storage.request_reconcile()
        if cam_id is not None:
            self.live.stop_camera(cam_id)
        if self._running:
            # Reabre só o que mudou (URL/senha nova, câmera removida ou com ao vivo desligado).
            self.hubs.sync()
        self.events.bump()
