"""Playlists HLS VOD a partir do índice de segmentos.

- Cada segmento gravado vira N "partes" em byte-range (alinhadas a keyframes), então o
  AVPlayer busca só alguns MB para fazer seek, sem remux nem transcodificação.
- ``EXT-X-PROGRAM-DATE-TIME`` em cada parte: o app usa ``AVPlayerItem.currentDate()`` para
  mostrar o horário real e ``seek(to: Date)`` para pular para um horário da linha do tempo.
- ``EXT-X-DISCONTINUITY`` em lacunas e trocas de execução do FFmpeg (timestamps reiniciam).
- H.264 é servido em MPEG-TS; H.265 em fMP4 com ``EXT-X-MAP`` (exigência da Apple).
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone

GAP_DISCONTINUITY = 1.5


def _pdt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_vod_playlist(segments: list[dict], url_for, start: float | None = None, end: float | None = None) -> str:
    """``url_for(seg)`` devolve a URL do arquivo do segmento (já com token de mídia)."""
    lines: list[str] = []
    max_dur = 1.0
    prev = None
    uses_fmp4 = False
    for seg in segments:
        parts = json.loads(seg["parts"]) if isinstance(seg["parts"], str) else seg["parts"]
        if not parts:
            parts = [[seg["init_len"], seg["size"] - seg["init_len"], seg["duration"]]]
        # Escala as durações das partes para bater com a duração real indexada.
        total = sum(p[2] for p in parts) or seg["duration"]
        scale = seg["duration"] / total if total else 1.0
        url = url_for(seg)
        discontinuity = prev is not None and (
            seg["start_ts"] - prev["end_ts"] > GAP_DISCONTINUITY
            or seg["run_id"] != prev["run_id"]
            or seg["container"] != prev["container"]
            or (seg["width"], seg["height"], seg["codec"]) != (prev["width"], prev["height"], prev["codec"])
        )
        if discontinuity:
            lines.append("#EXT-X-DISCONTINUITY")
        if seg["container"] == "mp4":
            uses_fmp4 = True
            lines.append(f'#EXT-X-MAP:URI="{url}",BYTERANGE="{seg["init_len"]}@0"')
        t = seg["start_ts"]
        for off, length, dur in parts:
            d = dur * scale
            if end is not None and t >= end:
                break
            if start is not None and t + d <= start:
                t += d
                continue
            max_dur = max(max_dur, d)
            lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_pdt(t)}")
            lines.append(f"#EXTINF:{d:.3f},")
            lines.append(f"#EXT-X-BYTERANGE:{int(length)}@{int(off)}")
            lines.append(url)
            t += d
        prev = seg
    head = [
        "#EXTM3U",
        f"#EXT-X-VERSION:{7 if uses_fmp4 else 4}",
        f"#EXT-X-TARGETDURATION:{math.ceil(max_dur)}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-INDEPENDENT-SEGMENTS",
    ]
    return "\n".join(head + lines + ["#EXT-X-ENDLIST", ""])
