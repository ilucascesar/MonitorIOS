"""Importa as câmeras configuradas no Agent DVR (somente leitura do objects.json).

As credenciais vão direto do arquivo do Agent DVR para o banco cifrado (DPAPI); nada é
impresso. As URLs RTSP são obtidas via ONVIF (GetStreamUri) em cada câmera, sem supor
caminhos fixos. O Agent DVR não é alterado.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

from .cameras.onvif import OnvifClient, OnvifError

log = logging.getLogger("agentdvr_import")

DEFAULT_OBJECTS = Path(r"C:\Program Files\Agent\Media\XML\objects.json")


def read_agent_cameras(path: Path = DEFAULT_OBJECTS) -> list[dict]:
    if not path.is_file():
        raise FileNotFoundError(f"objects.json do Agent DVR não encontrado em {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    out = []
    for cam in data.get("cameras", []):
        s = cam.get("settings", {})
        ident = s.get("onvifident") or ""
        p = urlsplit(ident) if ident else None
        host = (p.hostname if p else "") or ""
        if not host:
            continue
        out.append({
            "agent_id": cam.get("id"),
            "name": cam.get("name") or host,
            "host": host,
            "onvif_port": (p.port if p and p.port else 80),
            "username": s.get("login") or "",
            "password": s.get("password") or "",
            "main_override": s.get("mainstreamoverride") or s.get("mainstream") or "",
            "sub_override": s.get("substreamoverride") or s.get("substream") or "",
        })
    return out


def choose_streams(profiles: list[dict]) -> tuple[dict | None, dict | None]:
    usable = [p for p in profiles if p.get("rtsp_url")]
    if not usable:
        return None, None
    usable.sort(key=lambda p: (p.get("width", 0) * p.get("height", 0)), reverse=True)
    main = usable[0]
    sub = usable[-1] if len(usable) > 1 and usable[-1]["rtsp_url"] != main["rtsp_url"] else None
    return main, sub


def import_cameras(runtime, path: Path = DEFAULT_OBJECTS) -> dict:
    existing = {(c["host"], c["onvif_port"]) for c in runtime.db.list_cameras()}
    report = {"imported": [], "skipped": [], "errors": []}
    for ac in read_agent_cameras(path):
        key = (ac["host"], ac["onvif_port"])
        if key in existing:
            report["skipped"].append({"host": ac["host"], "reason": "já cadastrada"})
            continue
        data = {
            "name": ac["name"] if not ac["name"].replace(".", "").isdigit() else f"Câmera {ac['host'].rsplit('.', 1)[-1]}",
            "host": ac["host"], "onvif_port": ac["onvif_port"], "username": ac["username"],
            "password": ac["password"],
        }
        onvif_info = None
        try:
            client = OnvifClient(ac["host"], ac["onvif_port"], ac["username"], ac["password"])
            onvif_info = client.probe()
            main, sub = choose_streams(onvif_info["profiles"])
            if main:
                data["main_url"] = main["rtsp_url"]
                data.update(codec=main.get("encoding", "").replace("h265", "hevc"), width=main.get("width", 0),
                            height=main.get("height", 0), fps=main.get("fps", 0))
                data["rtsp_port"] = urlsplit(main["rtsp_url"]).port or 554
            if sub:
                data["sub_url"] = sub["rtsp_url"]
                data.update(sub_codec=sub.get("encoding", "").replace("h265", "hevc"), sub_width=sub.get("width", 0),
                            sub_height=sub.get("height", 0))
        except OnvifError as e:
            report["errors"].append({"host": ac["host"], "error": str(e)})
        if not data.get("main_url") and ac["main_override"].lower().startswith("rtsp"):
            data["main_url"] = ac["main_override"]
        if not data.get("main_url"):
            data["record_enabled"] = False
        try:
            cam = runtime.cameras.create(data)
        except ValueError as e:
            report["errors"].append({"host": ac["host"], "error": str(e)})
            continue
        report["imported"].append({
            "id": cam["id"], "name": cam["name"], "host": cam["host"], "main_url": cam["main_url"],
            "sub_url": cam["sub_url"], "codec": cam["codec"], "resolution": f"{cam['width']}x{cam['height']}",
            "profiles": [
                {k: p.get(k) for k in ("name", "encoding", "width", "height", "fps")} for p in (onvif_info or {}).get("profiles", [])
            ],
        })
    return report
