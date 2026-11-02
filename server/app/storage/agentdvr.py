"""Gravações do Agent DVR na linha do tempo, somente leitura.

Durante a migração o Agent DVR continua gravando em ``<mídia>\\video``. Os arquivos dele são
indexados com ``location='agentdvr'`` para o app mostrar as horas anteriores ao MonitorIOS:

- nada é movido, renomeado ou apagado (a retenção ignora essa localização e ``.mkv`` nem é
  extensão de mídia própria); só a linha do índice sai quando o arquivo some ou fica velho;
- a câmera é identificada pelo id do Agent DVR no nome (``7_01-11-2026_22-04-55_153.mkv``)
  e pelo host em ``objects.json``, o mesmo critério da importação de câmeras;
- onde existe gravação própria, ela tem prioridade (``timeline.prefer_own``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from ..recordings.indexer import file_mtime, probe_segment

log = logging.getLogger("storage.agentdvr")

LOCATION = "agentdvr"
_NAME = re.compile(r"^(\d+)_(\d{2}-\d{2}-\d{4}_\d{2}-\d{2}-\d{2})_(\d{3})\.mkv$", re.I)
_SKIP_DIRS = {"thumbs", "grabs", "$recycle.bin"}
# Arquivo ainda sendo escrito pelo Agent DVR (até 15 min por arquivo): espera ficar parado.
SETTLE_SECONDS = 120


def parse_name(name: str) -> tuple[int, float] | None:
    """(id da câmera no Agent DVR, início em epoch) a partir do nome do arquivo."""
    m = _NAME.match(name)
    if not m:
        return None
    try:
        start = datetime.strptime(m.group(2), "%d-%m-%Y_%H-%M-%S").timestamp() + int(m.group(3)) / 1000
    except ValueError:
        return None
    return int(m.group(1)), start


def agent_hosts(objects_path: Path) -> dict[int, str]:
    """id da câmera no Agent DVR -> host (só leitura; credenciais do arquivo não são usadas)."""
    data = json.loads(Path(objects_path).read_text(encoding="utf-8-sig"))
    out = {}
    for cam in data.get("cameras", []):
        ident = (cam.get("settings") or {}).get("onvifident") or ""
        host = urlsplit(ident).hostname if ident else None
        if host and cam.get("id") is not None:
            out[int(cam["id"])] = host
    return out


class AgentDvrIndex:
    def __init__(self, cfg, db, events):
        self.cfg = cfg
        self.db = db
        self.events = events
        self._failed: dict[str, float] = {}  # rel -> mtime de arquivos que o ffprobe não leu

    def scan(self, limit: int = 300) -> dict:
        result = {"added": 0, "removed": 0}
        s = self.cfg.settings
        if not s.agentdvr_playback:
            removed = self.db.execute("DELETE FROM segments WHERE location=?", (LOCATION,)).rowcount
            result["removed"] = removed
            return result
        root = self.cfg.agentdvr_root
        if not os.path.isdir(root):
            return result
        try:
            hosts = agent_hosts(Path(s.agentdvr_objects))
        except (OSError, ValueError) as e:
            log.warning("objects.json do Agent DVR ilegível: %s", e)
            return result
        cam_by_host: dict[str, dict] = {}
        for c in self.db.list_cameras(include_deleted=True):
            # Câmera ativa vence uma removida com o mesmo host.
            if c["host"] not in cam_by_host or not c["deleted"]:
                cam_by_host[c["host"]] = c
        cutoff = time.time() - s.retention_days * 86400
        known = self.db.known_paths(LOCATION)
        seen: set[str] = set()
        walk_errors: list[OSError] = []
        for dirpath, dirs, files in os.walk(root, onerror=walk_errors.append):
            dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS]
            for name in files:
                parsed = parse_name(name)
                if not parsed:
                    continue
                p = Path(dirpath) / name
                rel = p.relative_to(root).as_posix()
                seen.add(rel)
                agent_id, start = parsed
                if rel in known or start < cutoff:
                    continue
                if result["added"] >= limit:
                    continue
                cam = cam_by_host.get(hosts.get(agent_id, ""))
                if not cam:
                    continue
                mtime = file_mtime(p)
                if time.time() - mtime < SETTLE_SECONDS or self._failed.get(rel) == mtime:
                    continue
                res = probe_segment(self.cfg.ffprobe, p, s.hls_part_seconds, timeout=180)
                if not res.ok:
                    self._failed[rel] = mtime
                    log.info("arquivo do Agent DVR ignorado (%s): %s", res.error, name)
                    continue
                self.db.insert_segment({
                    "camera_id": cam["id"], "run_id": f"adv-{p.stem}", "location": LOCATION, "path": rel,
                    "container": res.container, "start_ts": start, "end_ts": start + res.duration,
                    "duration": res.duration, "size": res.size, "codec": res.codec, "width": res.width,
                    "height": res.height, "parts": res.parts, "status": "ok",
                })
                result["added"] += 1
        # Só tira do índice o que sumiu se a pasta foi lida até o fim (SMB oscilando não apaga nada).
        if not walk_errors and os.path.isdir(root):
            for rel in known - seen:
                self.db.execute("DELETE FROM segments WHERE location=? AND path=?", (LOCATION, rel))
                result["removed"] += 1
        result["removed"] += self.db.execute(
            "DELETE FROM segments WHERE location=? AND end_ts < ? AND protected=0", (LOCATION, cutoff)).rowcount
        if result["added"]:
            self.events.emit("info", "agentdvr", f"gravações do Agent DVR: {result['added']} arquivo(s) adicionado(s) à linha do tempo")
        return result
