"""Eventos operacionais (câmera offline, armazenamento indisponível, perda de dados...)."""
from __future__ import annotations

import logging
import threading

from .database import Database

log = logging.getLogger("events")
_LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING, "error": logging.ERROR}


class EventBus:
    def __init__(self, db: Database):
        self.db = db
        self.version = 0
        self._lock = threading.Lock()

    def emit(self, level: str, kind: str, message: str, camera_id: int | None = None) -> None:
        log.log(_LEVELS.get(level, logging.INFO), message, extra={"ctx": {"kind": kind, "camera": camera_id}})
        try:
            self.db.add_event(level, kind, message, camera_id)
        except Exception:  # pragma: no cover - banco indisponível não pode derrubar a gravação
            log.exception("falha ao registrar evento")
        with self._lock:
            self.version += 1

    def bump(self) -> None:
        with self._lock:
            self.version += 1
