from __future__ import annotations

import asyncio
import json
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from ..cameras.urls import build_stream_url
from ..security.auth import Principal
from ..streaming.hub import Subscriber
from ..streaming.live import SAFE_NAME
from .deps import current_principal, media_principal, playlist_response, rt

router = APIRouter(prefix="/api/v1", tags=["live"])


def _quality(q: str) -> str:
    if q not in ("main", "sub"):
        raise HTTPException(422, "qualidade deve ser main ou sub")
    return q


def _start(request: Request, cam_id: int, quality: str):
    runtime = rt(request)
    cam = runtime.db.get_camera(cam_id)
    if not cam:
        raise HTTPException(404, "câmera não encontrada")
    if not cam["live_enabled"]:
        raise HTTPException(403, "visualização ao vivo desabilitada para esta câmera")
    if quality == "sub" and not cam["sub_url"]:
        quality = "main"
    url = build_stream_url(cam, quality)
    if not url and cam_id not in runtime.live.input_overrides:
        raise HTTPException(409, "câmera sem URL RTSP configurada")
    codec = (cam["sub_codec"] if quality == "sub" else cam["codec"]) or cam["codec"] or "h264"
    return runtime.live.ensure(cam_id, quality, url, codec), quality


@router.get("/cameras/{cam_id}/live")
def live_info(cam_id: int, request: Request, quality: str = "sub", p: Principal = Depends(current_principal)):
    runtime = rt(request)
    _, quality = _start(request, cam_id, _quality(quality))
    ttl = runtime.cfg.settings.media_token_ttl_minutes * 60
    mt = runtime.auth.issue_media_token(p, f"cam:{cam_id}", ttl)
    return {
        "playlist_url": f"/api/v1/live/{cam_id}/{quality}/index.m3u8?{urlencode({'mt': mt})}",
        "quality": quality,
        "media_token": mt,
        "expires_at": time.time() + ttl,
    }


@router.websocket("/live/{cam_id}/ws")
async def live_ws(websocket: WebSocket, cam_id: int, quality: str = "main"):
    """Ao vivo de baixa latência (ver ``streaming.hub``). Autenticação: ``Authorization: Bearer``
    ou token de mídia ``?mt=`` com escopo da câmera."""
    runtime = websocket.app.state.rt
    h = websocket.headers.get("authorization", "")
    principal = runtime.auth.authenticate(h[7:].strip()) if h.lower().startswith("bearer ") else None
    mt = websocket.query_params.get("mt", "")
    if not principal and mt:
        principal = runtime.auth.verify_media_token(mt, f"cam:{cam_id}")
    if not principal:
        await websocket.close(code=4401)
        return
    cam = runtime.db.get_camera(cam_id)
    if not cam:
        await websocket.close(code=4404)
        return
    if not cam["live_enabled"]:
        await websocket.close(code=4403)
        return
    try:
        hub = await asyncio.to_thread(runtime.hubs.get, cam, quality if quality in ("main", "sub") else "main")
    except ValueError:
        await websocket.close(code=4409)
        return
    await websocket.accept()
    sub = Subscriber(asyncio.get_running_loop())
    q = hub.key[1]
    await websocket.send_text(json.dumps({
        "type": "info", "camera_id": cam_id, "quality": q, "state": hub.state,
        "width": cam["sub_width"] if q == "sub" else cam["width"],
        "height": cam["sub_height"] if q == "sub" else cam["height"],
        "fps": cam["fps"], "server_time": time.time(),
    }))
    hub.subscribe(sub)

    async def receiver():
        # O app só manda pings/fechamento; ao desconectar, encerra o envio.
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
        except (WebSocketDisconnect, RuntimeError):
            pass
        sub.close()

    recv = asyncio.create_task(receiver())
    try:
        while True:
            msg = await sub.next()
            if msg is None:
                break
            if isinstance(msg, str):
                await websocket.send_text(msg)
            else:
                await websocket.send_bytes(msg)
    except (WebSocketDisconnect, RuntimeError, OSError):
        pass
    finally:
        sub.closed = True
        hub.unsubscribe(sub)
        recv.cancel()
        try:
            await websocket.close()
        except RuntimeError:
            pass


@router.get("/live/{cam_id}/{quality}/index.m3u8")
def live_playlist(cam_id: int, quality: str, request: Request):
    media_principal(request, f"cam:{cam_id}")
    runtime = rt(request)
    st = runtime.live.get(cam_id, _quality(quality))
    if not st or not st.alive():
        st, _q = _start(request, cam_id, quality)
    if not runtime.live.wait_ready(st, 20):
        detail = " | ".join(list(st.stderr_tail)[-2:]) or "câmera não respondeu"
        raise HTTPException(504, f"fluxo ao vivo indisponível: {detail}"[:300])
    mt = request.query_params.get("mt", "")
    suffix = f"?{urlencode({'mt': mt})}" if mt else ""
    out = []
    for line in st.playlist.read_text(encoding="utf-8", errors="replace").splitlines():
        if line and not line.startswith("#"):
            line = f"{line}{suffix}"
        elif line.startswith("#EXT-X-MAP:URI=") and suffix:
            line = line.replace('init.mp4"', f'init.mp4{suffix}"')
        out.append(line)
    return playlist_response(request, "\n".join(out) + "\n")


@router.get("/live/{cam_id}/{quality}/{name}")
def live_file(cam_id: int, quality: str, name: str, request: Request):
    media_principal(request, f"cam:{cam_id}")
    if not SAFE_NAME.match(name):
        raise HTTPException(404, "arquivo inválido")
    st = rt(request).live.get(cam_id, _quality(quality))
    if not st:
        raise HTTPException(404, "fluxo não iniciado")
    path = st.dir / name
    if not path.is_file():
        raise HTTPException(404, "segmento expirado")
    mime = "video/mp2t" if name.endswith(".ts") else "video/mp4"
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "no-cache"})
