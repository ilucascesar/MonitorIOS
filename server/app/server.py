"""Execução do servidor HTTP (uvicorn) — usado pela CLI e pelo serviço do Windows."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

import uvicorn

from .logging_setup import setup_logging
from .main import create_app
from .runtime import Runtime

log = logging.getLogger("server")


class ServerHandle:
    def __init__(self, data_dir: Path | None = None, console: bool = True):
        self.runtime = Runtime(data_dir)
        s = self.runtime.cfg.settings
        setup_logging(self.runtime.cfg.logs_dir, s.log_level, console=console)
        app = create_app(self.runtime)
        ssl = {}
        if s.tls_certfile and s.tls_keyfile:
            ssl = {"ssl_certfile": s.tls_certfile, "ssl_keyfile": s.tls_keyfile}
        config = uvicorn.Config(
            app, host=s.http_host, port=s.http_port, log_config=None, access_log=False,
            proxy_headers=False, timeout_graceful_shutdown=10, ws_ping_interval=20, ws_ping_timeout=20, **ssl,
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        """Bloqueia até ``stop()``."""
        self.server.run()

    def run_in_thread(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="uvicorn", daemon=True)
        t.start()
        return t

    def stop(self) -> None:
        self.server.should_exit = True
