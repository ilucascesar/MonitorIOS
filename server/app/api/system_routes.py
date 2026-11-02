from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .. import __version__
from ..cameras.onvif import OnvifError, discover
from ..logging_setup import memory_handler
from ..security.auth import Principal
from .deps import COOKIE_NAME, current_principal, require_admin, rt
from .status import full_status, system_metrics

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/health")
def health():
    return {"ok": True, "version": __version__}


@router.get("/system/status")
def status(request: Request, _: Principal = Depends(current_principal)):
    return full_status(rt(request))


@router.get("/system/storage")
def storage(request: Request, _: Principal = Depends(current_principal)):
    runtime = rt(request)
    by_cam = runtime.db.usage_by_camera()
    names = {c["id"]: c["name"] for c in runtime.db.list_cameras(include_deleted=True)}
    for r in by_cam:
        r["name"] = names.get(r["camera_id"], f"#{r['camera_id']}")
    return {**runtime.storage.state, "by_camera": by_cam}


@router.get("/system/events")
def events(request: Request, limit: int = 100, _: Principal = Depends(current_principal)):
    return rt(request).db.recent_events(min(max(limit, 1), 1000))


@router.websocket("/ws")
async def ws_status(websocket: WebSocket):
    runtime = websocket.app.state.rt
    token = ""
    h = websocket.headers.get("authorization", "")
    if h.lower().startswith("bearer "):
        token = h[7:].strip()
    token = token or websocket.cookies.get(COOKIE_NAME, "")
    principal = runtime.auth.authenticate(token)
    if not principal:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    last_version, ticks = -1, 0
    try:
        while True:
            version = runtime.events.version
            if version != last_version or ticks % 5 == 0:
                payload = await asyncio.to_thread(full_status, runtime)
                await websocket.send_text(json.dumps({"type": "status", "data": payload}, default=str))
                last_version = version
            ticks += 1
            try:
                # Aceita pings do cliente; também detecta desconexão.
                await asyncio.wait_for(websocket.receive_text(), timeout=2)
            except asyncio.TimeoutError:
                pass
    except WebSocketDisconnect:
        return
    except RuntimeError:
        return


# ------------------------------------------------------------------ admin
admin = APIRouter(prefix="/api/v1/admin", tags=["admin"])


@admin.get("/settings")
def get_settings(request: Request, _: Principal = Depends(require_admin)):
    return rt(request).cfg.settings.model_dump()


@admin.put("/settings")
def put_settings(changes: dict, request: Request, _: Principal = Depends(require_admin)):
    runtime = rt(request)
    try:
        new = runtime.cfg.update(changes)
    except ValueError as e:
        raise HTTPException(422, str(e))
    runtime.events.emit("info", "settings", f"configurações alteradas: {', '.join(sorted(changes))}")
    if {"segment_seconds", "stall_timeout_seconds", "ffmpeg_path", "buffer_dir"} & set(changes):
        runtime.supervisor.sync()
    return new.model_dump()


class SmbCreds(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(max_length=256)
    share: str | None = None
    storage_root: str | None = None


@admin.post("/storage/credentials")
def set_smb(body: SmbCreds, request: Request, _: Principal = Depends(require_admin)):
    runtime = rt(request)
    changes = {}
    if body.share:
        changes["smb_share"] = body.share
    if body.storage_root:
        changes["storage_root"] = body.storage_root
    if changes:
        runtime.cfg.update(changes)
    runtime.storage.set_smb_credentials(body.username, body.password)
    return runtime.storage.check_health()


@admin.post("/storage/test")
def storage_test(request: Request, _: Principal = Depends(require_admin)):
    return rt(request).storage.check_health()


@admin.post("/maintenance/{task}")
def maintenance(task: str, request: Request, _: Principal = Depends(require_admin)):
    runtime = rt(request)
    if task == "retention":
        return runtime.storage.run_retention()
    if task == "reconcile":
        return {"buffer": runtime.supervisor.reconcile_buffer(), **runtime.storage.reconcile_remote()}
    raise HTTPException(404, "tarefa desconhecida")


@admin.get("/logs")
def logs(lines: int = 300, _: Principal = Depends(require_admin)):
    buf = list(memory_handler.buffer)[-min(max(lines, 1), 2000):]
    return [json.loads(x) if x.startswith("{") else {"msg": x} for x in buf]


@admin.get("/system")
def admin_system(request: Request, _: Principal = Depends(require_admin)):
    runtime = rt(request)
    return {**system_metrics(runtime), "recorders": runtime.supervisor.status(), "ffmpeg": runtime.cfg.ffmpeg,
            "data_dir": str(runtime.cfg.data_dir)}


class UserIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=256)
    role: str = "viewer"


@admin.get("/users")
def users(request: Request, _: Principal = Depends(require_admin)):
    return rt(request).auth.list_users()


@admin.post("/users", status_code=201)
def create_user(body: UserIn, request: Request, _: Principal = Depends(require_admin)):
    try:
        uid = rt(request).auth.create_user(body.username, body.password, body.role)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception:
        raise HTTPException(409, "usuário já existe")
    return {"id": uid}


@admin.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request, p: Principal = Depends(require_admin)):
    if user_id == p.user_id:
        raise HTTPException(422, "não é possível remover o próprio usuário")
    rt(request).auth.delete_user(user_id)
    return {"ok": True}


@admin.post("/onvif/discover")
def onvif_discover(request: Request, _: Principal = Depends(require_admin)):
    known = {c["host"] for c in rt(request).db.list_cameras()}
    found = discover(3.0)
    for d in found:
        d["registered"] = d["host"] in known
    return found


class OnvifProbeIn(BaseModel):
    host: str
    port: int = 80
    username: str = ""
    password: str = ""


@admin.post("/onvif/probe")
def onvif_probe(body: OnvifProbeIn, request: Request, _: Principal = Depends(require_admin)):
    try:
        return rt(request).cameras.onvif_probe(body.host, body.port, body.username, body.password)
    except OnvifError as e:
        raise HTTPException(502, str(e))


class RtspProbeIn(BaseModel):
    url: str
    username: str = ""
    password: str = ""


@admin.post("/rtsp/probe")
def rtsp_probe(body: RtspProbeIn, request: Request, _: Principal = Depends(require_admin)):
    if not body.url.lower().startswith(("rtsp://", "rtsps://")):
        raise HTTPException(422, "URL deve começar com rtsp://")
    return rt(request).cameras.probe_rtsp(body.url, body.username, body.password)


@admin.post("/import/agentdvr")
def import_agentdvr(request: Request, _: Principal = Depends(require_admin)):
    from ..agentdvr_import import import_cameras

    runtime = rt(request)
    try:
        result = import_cameras(runtime)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    runtime.apply_camera_changes()
    return result
