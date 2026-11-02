"""Estado consolidado (usado por /system/status e pelo WebSocket)."""
from __future__ import annotations

import os
import time

import psutil

from ..runtime import Runtime

_proc = psutil.Process(os.getpid())
_proc.cpu_percent(None)


def camera_states(runtime: Runtime) -> list[dict]:
    rec = runtime.supervisor.status()
    live = {a["camera_id"] for a in runtime.live.active()}
    out = []
    for cam in runtime.db.list_cameras():
        r = rec.get(cam["id"])
        reach = runtime.cameras.reachable.get(cam["id"], {})
        latest = runtime.db.latest_segment(cam["id"])
        recording = bool(r and r["state"] == "recording")
        online = bool(r and r["online"]) or (r is None and bool(reach.get("reachable")))
        last_seen = (r or {}).get("last_data_at") if online else (latest["end_ts"] if latest else None)
        out.append({
            "id": cam["id"],
            "name": cam["name"],
            "online": online,
            "recording": recording,
            "record_enabled": bool(cam["record_enabled"]),
            "live_enabled": bool(cam["live_enabled"]),
            "recorder_state": (r or {}).get("state", "disabled"),
            "last_seen": last_seen,
            "last_segment_end": latest["end_ts"] if latest else None,
            "last_error": (r or {}).get("last_error", "") if not recording else "",
            "restarts": (r or {}).get("restarts", 0),
            "reachable": reach.get("reachable"),
            "live_active": cam["id"] in live,
            "codec": cam["codec"], "width": cam["width"], "height": cam["height"],
        })
    return out


def system_metrics(runtime: Runtime) -> dict:
    vm = psutil.virtual_memory()
    children = _proc.children(recursive=True)
    ff_mem = 0
    for c in children:
        try:
            ff_mem += c.memory_info().rss
        except psutil.Error:
            pass
    try:
        ddu = psutil.disk_usage(str(runtime.cfg.data_dir))
        local_disk = {"total_bytes": ddu.total, "free_bytes": ddu.free, "percent": ddu.percent}
    except OSError:
        local_disk = None
    return {
        "version": runtime.version,
        "time": time.time(),
        "uptime_seconds": round(time.time() - runtime.started_at),
        "cpu_percent": psutil.cpu_percent(None),
        "process_cpu_percent": _proc.cpu_percent(None),
        "memory_percent": vm.percent,
        "process_memory_bytes": _proc.memory_info().rss,
        "ffmpeg_processes": len(children),
        "ffmpeg_memory_bytes": ff_mem,
        "local_disk": local_disk,
    }


def full_status(runtime: Runtime) -> dict:
    return {
        "server": system_metrics(runtime),
        "storage": runtime.storage.state,
        "cameras": camera_states(runtime),
        "live": runtime.live.active(),
        "events_version": runtime.events.version,
    }
