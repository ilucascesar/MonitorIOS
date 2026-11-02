from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from ..recordings import timeline as tl
from ..recordings.indexer import legacy_chunk
from ..security.auth import Principal
from ..streaming.hls import build_vod_playlist
from .deps import current_principal, media_principal, playlist_response, rt

router = APIRouter(prefix="/api/v1", tags=["recordings"])

MAX_RANGE = 3 * 86400


def _tz(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise HTTPException(422, "fuso horÃ¡rio invÃ¡lido")


def _cam(request: Request, cam_id: int) -> dict:
    cam = rt(request).db.get_camera(cam_id)
    if not cam:
        # CÃ¢meras removidas continuam consultÃ¡veis atÃ© a retenÃ§Ã£o apagar as gravaÃ§Ãµes.
        cam = rt(request).db.one("SELECT * FROM cameras WHERE id=?", (cam_id,))
    if not cam:
        raise HTTPException(404, "cÃ¢mera nÃ£o encontrada")
    return cam


def _check_range(start: float, end: float) -> None:
    if end <= start:
        raise HTTPException(422, "end deve ser maior que start")
    if end - start > MAX_RANGE:
        raise HTTPException(422, "intervalo mÃ¡ximo de 3 dias")


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
    """Devolve a URL da playlist (com token de mÃ­dia) e os intervalos para a linha do tempo."""
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
        raise HTTPException(404, "sem gravaÃ§Ãµes no intervalo")
    mt = request.query_params.get("mt", "")
    suffix = f"?{urlencode({'mt': mt})}" if mt else ""
    def url_for(s: dict, part: int | None = None) -> str:
        if part is not None:
            return f"/api/v1/media/{s['id']}/{part}.ts{suffix}"
        return f"/api/v1/media/{s['id']}.{s['container']}{suffix}"

    text = build_vod_playlist(segs, url_for, start, end)
    return playlist_response(request, text)


class _ChunkCache:
    """Ãšltimos trechos remuxados do Agent DVR em memÃ³ria (o AVPlayer pede a mesma parte de novo ao
    voltar alguns segundos na linha do tempo)."""

    def __init__(self, max_bytes: int = 64 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._items: OrderedDict[tuple[int, int], bytes] = OrderedDict()
        self._size = 0
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            data = self._items.get(key)
            if data is not None:
                self._items.move_to_end(key)
            return data

    def put(self, key, data: bytes) -> None:
        with self._lock:
            if key in self._items:
                return
            self._items[key] = data
            self._size += len(data)
            while self._size > self.max_bytes and self._items:
                _k, old = self._items.popitem(last=False)
                self._size -= len(old)


_chunks = _ChunkCache()
_ahead = ThreadPoolExecutor(max_workers=1, thread_name_prefix="chunk-ahead")
_ahead_pending: set[tuple[int, int]] = set()


def _make_chunk(runtime, seg: dict, parts: list, part: int) -> bytes:
    path = runtime.storage.resolve(seg)
    if not path.is_file():
        raise FileNotFoundError(path)
    nxt = parts[part + 1][0] if part + 1 < len(parts) else None
    return legacy_chunk(runtime.cfg.ffmpeg, path, parts[part][0], nxt)


def _chunk_ahead(runtime, seg: dict, parts: list, part: int) -> None:
    key = (seg["id"], part)
    try:
        if _chunks.get(key) is None:
            _chunks.put(key, _make_chunk(runtime, seg, parts, part))
    except Exception:  # noqa: BLE001 - adiantamento Ã© opcional
        pass
    finally:
        _ahead_pending.discard(key)


@router.get("/media/{seg_id}/{part}.ts")
def legacy_media_part(seg_id: int, part: int, request: Request):
    """Trecho de uma gravaÃ§Ã£o do Agent DVR, remuxado de MKV para MPEG-TS sem recodificar."""
    runtime = rt(request)
    seg = runtime.db.get_segment(seg_id)
    if not seg or seg["container"] != "mkv":
        raise HTTPException(404, "segmento nÃ£o encontrado")
    media_principal(request, f"cam:{seg['camera_id']}")
    parts = json.loads(seg["parts"] or "[]")
    if not 0 <= part < len(parts):
        raise HTTPException(404, "parte nÃ£o encontrada")
    headers = {"Cache-Control": "private, max-age=86400"}
    runtime.storage.mirror.prefetch_after(seg)
    data = _chunks.get((seg_id, part))
    if data is None:
        try:
            data = _make_chunk(runtime, seg, parts, part)
        except ValueError:
            raise HTTPException(404, "segmento nÃ£o encontrado")
        except FileNotFoundError:
            raise HTTPException(503, "arquivo indisponÃ­vel (armazenamento offline?)")
        except (OSError, subprocess.TimeoutExpired):
            raise HTTPException(503, "nÃ£o foi possÃ­vel ler a gravaÃ§Ã£o do Agent DVR")
        _chunks.put((seg_id, part), data)
    # Os dois trechos seguintes ficam prontos antes de o player pedir.
    for nxt in (part + 1, part + 2):
        key = (seg_id, nxt)
        if nxt < len(parts) and key not in _ahead_pending and _chunks.get(key) is None:
            _ahead_pending.add(key)
            _ahead.submit(_chunk_ahead, runtime, seg, parts, nxt)
    return Response(content=data, media_type="video/mp2t", headers=headers)


@router.get("/media/{seg_id}.{ext}")
def media_segment(seg_id: int, ext: str, request: Request):
    runtime = rt(request)
    seg = runtime.db.get_segment(seg_id)
    if not seg or ext != seg["container"]:
        raise HTTPException(404, "segmento nÃ£o encontrado")
    media_principal(request, f"cam:{seg['camera_id']}")
    try:
        # Primeiro trecho pedido de um segmento que sÃ³ estÃ¡ no pendrive: copia o arquivo inteiro
        # (~5 MB) para o PC; os demais trechos e os prÃ³ximos minutos jÃ¡ saem do disco local.
        runtime.storage.mirror.ensure(seg)
        runtime.storage.mirror.prefetch_after(seg)
        path = runtime.storage.resolve(seg)
    except ValueError:
        raise HTTPException(404, "segmento nÃ£o encontrado")
    if not path.is_file():
        raise HTTPException(503 if seg["location"] == "remote" else 404, "arquivo indisponÃ­vel (armazenamento offline?)")
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
        raise HTTPException(404, "exportaÃ§Ã£o nÃ£o encontrada")
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
        raise HTTPException(404, "exportaÃ§Ã£o nÃ£o encontrada")
    path = runtime.exports.file_path(job_id)
    if not path:
        raise HTTPException(409, "exportaÃ§Ã£o ainda nÃ£o concluÃ­da")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@router.delete("/exports/{job_id}")
def delete_export(job_id: str, request: Request, p: Principal = Depends(current_principal)):
    runtime = rt(request)
    job = runtime.exports.get(job_id)
    if not job or (job["user_id"] != p.user_id and not p.is_admin):
        raise HTTPException(404, "exportaÃ§Ã£o nÃ£o encontrada")
    runtime.exports.delete(job_id)
    return {"ok": True}
