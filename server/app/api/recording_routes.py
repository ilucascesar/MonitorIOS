from __future__ import annotations

import time
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..recordings import timeline as tl
from ..security.auth import Principal
from ..streaming.hls import build_vod_playlist
from .deps import current_principal, media_principal, playlist_response, rt

router = APIRouter(prefix="/api/v1", tags=["recordings"])

MAX_RANGE = 3 * 86400


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "fuso horário inválido")


def _cam(request: Request, cam_id: int) -> dict:
    cam = rt(request).db.get_camera(cam_id)
    if not cam:
        # Câmeras removidas continuam consultáveis até a retenção apagar as gravações.
        cam = rt(request).db.one("SELECT * FROM cameras WHERE id=?", (cam_id,))
    if not cam:
        raise HTTPException(404, "câmera não encontrada")
    return cam


def _check_range(start: float, end: float) -> None:
    if end <= start:
        raise HTTPException(422, "end deve ser maior que start")
    if end - start > MAX_RANGE:
        raise HTTPException(422, "intervalo máximo de 3 dias")


@router.get("/cameras/{cam_id}/days")
def recorded_days(cam_id: int, request: Request, month: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
                  tz: str = "America/Sao_Paulo", _: Principal = Depends(current_principal)):
    _cam(request, cam_id)
    zone = _tz(tz)
    year, mon = map(int, month.split("-"))
    days = tl.month_days(year, mon)
    start, _e = tl.day_bounds(days[0], zone)
    _s, end = tl.day_bounds(days[-1], zone)
    spans = rt(request).db.segment_starts(cam_id, start, end)
    per_day = tl.recorded_seconds_per_day(spans, year, mon, zone)
    return {"camera_id": cam_id, "month": month, "tz": tz,
            "days": [{"date": d, "recorded_seconds": s} for d, s in sorted(per_day.items())]}


@router.get("/cameras/{cam_id}/timeline")
def timeline(cam_id: int, request: Request, start: float, end: float, _: Principal = Depends(current_principal)):
    _cam(request, cam_id)
    _check_range(start, end)
    spans = rt(request).db.segment_starts(cam_id, start, end)
    merged = tl.clip(tl.merge_intervals(spans), start, end)
    return {
        "camera_id": cam_id, "start": start, "end": end,
        "intervals": [[round(a, 3), round(b, 3)] for a, b in merged],
        "gaps": [[round(a, 3), round(b, 3)] for a, b in tl.gaps(merged, start, min(end, time.time()))],
        "recorded_seconds": round(sum(b - a for a, b in merged), 1),
        "segments": len(spans),
    }


@router.get("/cameras/{cam_id}/locate")
def locate(cam_id: int, request: Request, t: float, _: Principal = Depends(current_principal)):
    _cam(request, cam_id)
    db = rt(request).db
    spans = db.segment_starts(cam_id, t - 3600, t + 3600)
    res = tl.locate(tl.merge_intervals(spans), t)
    if not res["recorded"] and res.get("next_available") is None:
        nxt = db.first_segment_after(cam_id, t)
        res["next_available"] = nxt["start_ts"] if nxt else None
    if not res["recorded"] and res.get("previous_end") is None:
        prv = db.last_segment_before(cam_id, t)
        res["previous_end"] = prv["end_ts"] if prv else None
    return {"camera_id": cam_id, "t": t, **res}


@router.get("/cameras/{cam_id}/playback")
def playback_info(cam_id: int, request: Request, start: float, end: float, p: Principal = Depends(current_principal)):
    """Devolve a URL da playlist (com token de mídia) e os intervalos para a linha do tempo."""
    runtime = rt(request)
    _cam(request, cam_id)
    _check_range(start, end)
    ttl = runtime.cfg.settings.media_token_ttl_minutes * 60
    mt = runtime.auth.issue_media_token(p, f"cam:{cam_id}", ttl)
    spans = runtime.db.segment_starts(cam_id, start, end)
    merged = tl.clip(tl.merge_intervals(spans), start, end)
    q = urlencode({"start": start, "end": end, "mt": mt})
    return {
        "playlist_url": f"/api/v1/cameras/{cam_id}/playback.m3u8?{q}",
        "snapshot_url": f"/api/v1/cameras/{cam_id}/snapshot.jpg?{urlencode({'mt': mt})}",
        "media_token": mt,
        "expires_at": time.time() + ttl,
        "intervals": [[round(a, 3), round(b, 3)] for a, b in merged],
    }


@router.get("/cameras/{cam_id}/playback.m3u8")
def playback_playlist(cam_id: int, request: Request, start: float, end: float):
    media_principal(request, f"cam:{cam_id}")
    _check_range(start, end)
    runtime = rt(request)
    segs = runtime.db.segments_in_range(cam_id, start, end)
    if not segs:
        raise HTTPException(404, "sem gravações no intervalo")
    mt = request.query_params.get("mt", "")
    suffix = f"?{urlencode({'mt': mt})}" if mt else ""
    text = build_vod_playlist(segs, lambda s: f"/api/v1/media/{s['id']}.{s['container']}{suffix}", start, end)
    return playlist_response(request, text)


@router.get("/media/{seg_id}.{ext}")
def media_segment(seg_id: int, ext: str, request: Request):
    runtime = rt(request)
    seg = runtime.db.get_segment(seg_id)
    if not seg or ext != seg["container"]:
        raise HTTPException(404, "segmento não encontrado")
    media_principal(request, f"cam:{seg['camera_id']}")
    try:
        path = runtime.storage.resolve(seg)
    except ValueError:
        raise HTTPException(404, "segmento não encontrado")
    if not path.is_file():
        raise HTTPException(503 if seg["location"] == "remote" else 404, "arquivo indisponível (armazenamento offline?)")
    mime = "video/mp2t" if ext == "ts" else "video/mp4"
    return FileResponse(path, media_type=mime, headers={"Cache-Control": "private, max-age=86400"})


class ProtectIn(BaseModel):
    start: float
    end: float
    protected: bool = True


@router.post("/cameras/{cam_id}/protect")
def protect(cam_id: int, body: ProtectIn, request: Request, p: Principal = Depends(current_principal)):
    _cam(request, cam_id)
    _check_range(body.start, body.end)
    n = rt(request).db.set_protected(cam_id, body.start, body.end, body.protected)
    rt(request).events.emit("info", "protect", f"{n} segmento(s) {'protegido(s)' if body.protected else 'desprotegido(s)'} por {p.username}", cam_id)
    return {"segments": n}


@router.get("/cameras/{cam_id}/protected")
def protected_ranges(cam_id: int, request: Request, start: float, end: float, _: Principal = Depends(current_principal)):
    rows = rt(request).db.query(
        "SELECT start_ts, end_ts FROM segments WHERE camera_id=? AND protected=1 AND end_ts>? AND start_ts<? ORDER BY start_ts",
        (cam_id, start, end),
    )
    merged = tl.merge_intervals([(r["start_ts"], r["end_ts"]) for r in rows])
    return {"intervals": [[a, b] for a, b in merged]}


# ------------------------------------------------------------------ exports
class ExportIn(BaseModel):
    camera_id: int
    start: float
    end: float


@router.post("/exports", status_code=202)
def create_export(body: ExportIn, request: Request, p: Principal = Depends(current_principal)):
    runtime = rt(request)
    _cam(request, body.camera_id)
    try:
        job = runtime.exports.create(body.camera_id, p.user_id, body.start, body.end)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except LookupError as e:
        raise HTTPException(404, str(e))
    return job


@router.get("/exports/{job_id}")
def get_export(job_id: str, request: Request, p: Principal = Depends(current_principal)):
    runtime = rt(request)
    job = runtime.exports.get(job_id)
    if not job or (job["user_id"] != p.user_id and not p.is_admin):
        raise HTTPException(404, "exportação não encontrada")
    if job["status"] == "done":
        mt = runtime.auth.issue_media_token(p, f"export:{job_id}", 3600)
        job["download_url"] = f"/api/v1/exports/{job_id}/download?{urlencode({'mt': mt})}"
    return job


@router.get("/exports/{job_id}/download")
def download_export(job_id: str, request: Request):
    p = media_principal(request, f"export:{job_id}")
    runtime = rt(request)
    job = runtime.exports.get(job_id)
    if not job or (job["user_id"] != p.user_id and not p.is_admin):
        raise HTTPException(404, "exportação não encontrada")
    path = runtime.exports.file_path(job_id)
    if not path:
        raise HTTPException(409, "exportação ainda não concluída")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.delete("/exports/{job_id}")
def delete_export(job_id: str, request: Request, p: Principal = Depends(current_principal)):
    runtime = rt(request)
    job = runtime.exports.get(job_id)
    if not job or (job["user_id"] != p.user_id and not p.is_admin):
        raise HTTPException(404, "exportação não encontrada")
    runtime.exports.delete(job_id)
    return {"ok": True}
