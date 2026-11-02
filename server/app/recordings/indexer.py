"""Análise de segmentos gravados com ffprobe.

Para cada arquivo calcula duração real (a partir dos pacotes de vídeo, não do nome nem do
tempo nominal de 60 s), codec, resolução e um índice de "partes" em byte-range alinhadas a
keyframes, usado pelas playlists HLS para que o seek no iPhone não precise baixar o
segmento inteiro.
"""
from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("recordings.indexer")

TS_PACKET = 188
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class ProbeResult:
    ok: bool
    duration: float = 0.0
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    size: int = 0
    container: str = "ts"
    parts: list[list[float]] = field(default_factory=list)  # [offset, length, duration]
    init_len: int = 0
    error: str = ""


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, timeout=timeout, creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL
    )


def _fps(rate: str) -> float:
    try:
        n, d = rate.split("/")
        return float(n) / float(d) if float(d) else 0.0
    except Exception:
        return 0.0


def probe_segment(ffprobe: str, path: Path, part_target: float = 10.0, timeout: float = 60) -> ProbeResult:
    try:
        size = path.stat().st_size
    except OSError as e:
        return ProbeResult(False, error=f"stat: {e}")
    if size < TS_PACKET * 10:
        return ProbeResult(False, size=size, error="arquivo vazio ou truncado")
    container = "mp4" if path.suffix.lower() == ".mp4" else "ts"
    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "packet=pts_time,dts_time,pos,flags:stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
        "-of", "json", str(path),
    ]
    try:
        cp = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        return ProbeResult(False, size=size, error="ffprobe timeout")
    except OSError as e:
        return ProbeResult(False, size=size, error=f"ffprobe indisponível: {e}")
    try:
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        data = {}
    streams = data.get("streams") or []
    packets = data.get("packets") or []
    if not streams or not packets:
        err = cp.stderr.decode("utf-8", "replace").strip().splitlines()
        return ProbeResult(False, size=size, container=container, error=(err[-1] if err else "sem vídeo")[:300])

    st = streams[0]
    fps = _fps(st.get("avg_frame_rate", "0/0")) or _fps(st.get("r_frame_rate", "0/0"))
    pts_list: list[float] = []
    keyframes: list[tuple[float, int]] = []
    for p in packets:
        t = p.get("pts_time") or p.get("dts_time")
        if t in (None, "N/A"):
            continue
        t = float(t)
        pts_list.append(t)
        if "K" in (p.get("flags") or ""):
            pos = p.get("pos")
            keyframes.append((t, int(pos) if pos not in (None, "N/A") else -1))
    if len(pts_list) < 2:
        return ProbeResult(False, size=size, container=container, error="poucos quadros")
    first = min(pts_list)
    frame_dur = 1.0 / fps if fps > 0 else 0.0
    end = max(pts_list) + frame_dur
    duration = max(0.0, end - first)
    keyframes.sort()

    if container == "ts":
        parts = _ts_parts(path, size, keyframes, first, end, part_target)
        init_len = 0
    else:
        parts, init_len = _mp4_parts(path, size, keyframes, first, end, part_target)

    return ProbeResult(
        ok=duration > 0.5,
        duration=round(duration, 3),
        codec=st.get("codec_name", ""),
        width=int(st.get("width") or 0),
        height=int(st.get("height") or 0),
        fps=round(fps, 2),
        size=size,
        container=container,
        parts=parts,
        init_len=init_len,
        error="" if duration > 0.5 else "duração muito curta",
    )


def _find_pat_before(f, pos: int) -> int:
    """Retorna o offset do pacote PAT (PID 0) imediatamente anterior a ``pos``, ou -1."""
    if pos <= 0 or pos % TS_PACKET:
        return -1
    look = 12
    start = max(0, pos - look * TS_PACKET)
    f.seek(start)
    buf = f.read(pos - start)
    for i in range(len(buf) - TS_PACKET, -1, -TS_PACKET):
        b = buf[i:i + 3]
        if len(b) == 3 and b[0] == 0x47 and (b[1] & 0x40) and (((b[1] & 0x1F) << 8) | b[2]) == 0:
            return start + i
    return -1


def _ts_parts(path: Path, size: int, keyframes, first: float, end: float, target: float) -> list[list[float]]:
    splits: list[tuple[int, float]] = [(0, first)]
    try:
        with open(path, "rb") as f:
            for t, pos in keyframes:
                if pos < 0 or t - splits[-1][1] < target or end - t < target * 0.3:
                    continue
                off = _find_pat_before(f, pos)
                if off > splits[-1][0]:
                    splits.append((off, t))
    except OSError as e:
        log.warning("falha ao ler %s para partes: %s", path.name, e)
        splits = [(0, first)]
    parts = []
    for i, (off, t) in enumerate(splits):
        nxt_off, nxt_t = (splits[i + 1] if i + 1 < len(splits) else (size, end))
        parts.append([off, nxt_off - off, round(max(0.001, nxt_t - t), 3)])
    return parts


def _iter_boxes(f, size: int):
    off = 0
    while off + 8 <= size:
        f.seek(off)
        hdr = f.read(16)
        if len(hdr) < 8:
            return
        box_size, kind = struct.unpack(">I4s", hdr[:8])
        if box_size == 1:
            if len(hdr) < 16:
                return
            box_size = struct.unpack(">Q", hdr[8:16])[0]
        elif box_size == 0:
            box_size = size - off
        if box_size < 8:
            return
        yield off, box_size, kind.decode("latin-1")
        off += box_size


def _mp4_parts(path: Path, size: int, keyframes, first: float, end: float, target: float):
    """fMP4 (usado para H.265): init = ftyp+moov; cada moof+mdat é um GOP."""
    frags: list[tuple[int, int]] = []
    init_len = 0
    try:
        with open(path, "rb") as f:
            boxes = list(_iter_boxes(f, size))
    except OSError:
        return [], 0
    pending_moof = None
    for off, bsize, kind in boxes:
        if kind == "moof":
            if not init_len:
                init_len = off
            pending_moof = off
        elif kind == "mdat" and pending_moof is not None:
            if off + bsize <= size:  # fragmento truncado é descartado
                frags.append((pending_moof, off + bsize))
            pending_moof = None
    if not init_len or not frags:
        return [], 0
    if len(keyframes) >= len(frags):
        times = [k[0] for k in keyframes[: len(frags)]]
    else:
        # Sem correspondência 1:1 — trata o arquivo inteiro como uma parte.
        return [[frags[0][0], frags[-1][1] - frags[0][0], round(end - first, 3)]], init_len
    groups: list[tuple[int, float]] = [(0, times[0])]
    for i in range(1, len(frags)):
        if times[i] - groups[-1][1] >= target and end - times[i] >= target * 0.3:
            groups.append((i, times[i]))
    parts = []
    for gi, (fi, t) in enumerate(groups):
        last_fi = groups[gi + 1][0] - 1 if gi + 1 < len(groups) else len(frags) - 1
        nxt_t = groups[gi + 1][1] if gi + 1 < len(groups) else end
        start_off = frags[fi][0]
        parts.append([start_off, frags[last_fi][1] - start_off, round(max(0.001, nxt_t - t), 3)])
    return parts, init_len


def probe_stream(ffprobe: str, url: str, timeout: float = 20) -> dict:
    """Consulta codec/resolução/fps de um fluxo RTSP (a URL contém credenciais: nunca logar)."""
    cmd = [
        ffprobe, "-v", "error", "-rtsp_transport", "tcp", "-timeout", str(int(timeout * 1_000_000)),
        "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,sample_rate",
        "-of", "json", url,
    ]
    try:
        cp = _run(cmd, timeout + 10)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "tempo esgotado ao conectar no RTSP"}
    except OSError as e:
        return {"ok": False, "error": f"ffprobe indisponível: {e}"}
    try:
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
    except json.JSONDecodeError:
        data = {}
    streams = data.get("streams") or []
    if not streams:
        from ..logging_setup import redact

        err = cp.stderr.decode("utf-8", "replace").strip().splitlines()
        msg = redact(err[-1] if err else "sem resposta")
        if "401" in msg or "Unauthorized" in msg:
            msg = "credenciais recusadas pela câmera (401)"
        return {"ok": False, "error": msg[:300]}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    out = {"ok": video is not None}
    if video:
        out.update(
            codec=video.get("codec_name", ""),
            width=int(video.get("width") or 0),
            height=int(video.get("height") or 0),
            fps=round(_fps(video.get("avg_frame_rate", "0/0")) or _fps(video.get("r_frame_rate", "0/0")), 2),
        )
    else:
        out["error"] = "fluxo sem vídeo"
    out["audio_codec"] = audio.get("codec_name", "") if audio else ""
    return out


def file_mtime(path: Path) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0
