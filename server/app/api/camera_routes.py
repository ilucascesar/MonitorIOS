from __future__ import annotations

import threading

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from ..security.auth import Principal
from ..streaming.snapshot import cached_snapshot
from .deps import current_principal, media_principal, require_admin, rt

router = APIRouter(prefix="/api/v1/cameras", tags=["cameras"])


class CameraIn(BaseModel):
    name: str | None = Field(None, max_length=64)
    host: str | None = Field(None, max_length=255)
    onvif_port: int | None = None
    rtsp_port: int | None = None
    username: str | None = Field(None, max_length=128)
    password: str | None = Field(None, max_length=256)
    main_url: str | None = Field(None, max_length=1024)
    sub_url: str | None = Field(None, max_length=1024)
    record_stream: str | None = None
    audio_mode: str | None = None
    record_enabled: bool | None = None
    live_enabled: bool | None = None
    sort_order: int | None = None


def _cam_or_404(request: Request, cam_id: int) -> dict:
    cam = rt(request).db.get_camera(cam_id)
    if not cam:
        raise HTTPException(404, "câmera não encontrada")
    return cam


@router.get("")
def list_cameras(request: Request, _: Principal = Depends(current_principal)):
    runtime = rt(request)
    return [runtime.cameras.public(c) for c in runtime.db.list_cameras()]


@router.get("/{cam_id}")
def get_camera(cam_id: int, request: Request, _: Principal = Depends(current_principal)):
    return rt(request).cameras.public(_cam_or_404(request, cam_id))


@router.post("", status_code=201)
def create_camera(body: CameraIn, request: Request, _: Principal = Depends(require_admin)):
    runtime = rt(request)
    try:
        cam = runtime.cameras.create(body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(422, str(e))
    runtime.apply_camera_changes(cam["id"])
    return cam


@router.patch("/{cam_id}")
def update_camera(cam_id: int, body: CameraIn, request: Request, _: Principal = Depends(require_admin)):
    runtime = rt(request)
    _cam_or_404(request, cam_id)
    try:
        cam = runtime.cameras.update(cam_id, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(422, str(e))
    # Aplicado sem reiniciar o servidor: só o gravador desta câmera é refeito se preciso.
    runtime.apply_camera_changes(cam_id)
    return cam


@router.delete("/{cam_id}")
def delete_camera(cam_id: int, request: Request, _: Principal = Depends(require_admin)):
    runtime = rt(request)
    _cam_or_404(request, cam_id)
    runtime.cameras.delete(cam_id)
    runtime.apply_camera_changes(cam_id)
    return {"ok": True}


@router.post("/{cam_id}/test")
def test_camera(cam_id: int, request: Request, _: Principal = Depends(require_admin)):
    runtime = rt(request)
    result = runtime.cameras.test_camera(_cam_or_404(request, cam_id))
    return result


@router.post("/{cam_id}/restart")
def restart_recording(cam_id: int, request: Request, _: Principal = Depends(require_admin)):
    runtime = rt(request)
    _cam_or_404(request, cam_id)
    if not runtime.supervisor.restart_camera(cam_id):
        threading.Thread(target=runtime.supervisor.sync, daemon=True).start()
    runtime.events.emit("info", "recorder", "reinício de gravação solicitado", cam_id)
    return {"ok": True}


@router.get("/{cam_id}/snapshot.jpg")
def snapshot(cam_id: int, request: Request):
    media_principal(request, f"cam:{cam_id}")
    runtime = rt(request)
    _cam_or_404(request, cam_id)
    # Keyframe atual da conexão ao vivo permanente: imagem de agora, sem abrir RTSP nem ler o pendrive.
    jpeg = runtime.hubs.snapshot(cam_id)
    if jpeg:
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=5"})
    src, key = None, None
    st = runtime.live.streams.get((cam_id, "sub")) or runtime.live.streams.get((cam_id, "main"))
    if st and st.alive():
        segs = sorted(st.dir.glob("seg_*.*"))
        if len(segs) >= 2:
            src, key = segs[-2], f"live{segs[-2].stem}"
    if src is None:
        seg = runtime.db.latest_segment(cam_id)
        if not seg:
            raise HTTPException(404, "sem imagem disponível")
        src, key = runtime.storage.resolve(seg), f"s{seg['id']}"
    out = cached_snapshot(runtime.cfg.ffmpeg, runtime.cfg.cache_dir, cam_id, key, src)
    if not out:
        raise HTTPException(503, "não foi possível gerar a miniatura")
    return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=30"})
