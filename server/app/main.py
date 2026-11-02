"""Aplicação FastAPI. Documentação interativa em /docs (somente a partir do próprio PC)."""
from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import auth_routes, camera_routes, live_routes, recording_routes, system_routes
from .api.deps import is_loopback
from .runtime import Runtime

access_log = logging.getLogger("http")
ADMIN_UI = Path(__file__).parent / "admin_ui"
_QUIET = ("/api/v1/media/", "/api/v1/live/")


def create_app(runtime: Runtime | None = None, data_dir: Path | None = None) -> FastAPI:
    rt = runtime or Runtime(data_dir)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        rt.start()
        try:
            yield
        finally:
            rt.stop()

    app = FastAPI(
        title="MonitorIOS API",
        version=__version__,
        description="Gravação contínua de câmeras IP, linha do tempo e streaming HLS para o app iOS.",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.rt = rt

    @app.middleware("http")
    async def guard_and_log(request: Request, call_next):
        path = request.url.path
        local_only = path.startswith(("/admin", "/docs", "/openapi.json"))
        if local_only and not rt.cfg.settings.admin_remote_access and not is_loopback(request):
            return JSONResponse({"detail": "painel disponível apenas no próprio servidor"}, status_code=403)
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            access_log.exception("erro não tratado", extra={"ctx": {"path": path}})
            return JSONResponse({"detail": "erro interno"}, status_code=500)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        level = logging.DEBUG if path.startswith(_QUIET) and response.status_code < 400 else logging.INFO
        # Só o caminho é logado (query strings podem conter tokens de mídia).
        access_log.log(level, f"{request.method} {path} {response.status_code}", extra={"ctx": {
            "ip": request.client.host if request.client else None,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }})
        return response

    for r in (auth_routes.router, camera_routes.router, recording_routes.router, live_routes.router,
              system_routes.router, system_routes.admin):
        app.include_router(r)

    if ADMIN_UI.is_dir():
        app.mount("/admin", StaticFiles(directory=ADMIN_UI, html=True), name="admin")

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/admin/")

    return app
