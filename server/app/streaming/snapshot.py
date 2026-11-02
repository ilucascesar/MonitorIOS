"""Miniaturas JPEG a partir do segmento mais recente (sem abrir nova conexão com a câmera)."""
from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_lock = threading.Lock()


def extract_frame(ffmpeg: str, src: Path, out: Path, width: int = 640, from_end: bool = True) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.stem + ".tmp.jpg")
    base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
    attempts = ([["-sseof", "-3"]] if from_end else []) + [[]]
    for seek in attempts:
        cmd = base + seek + ["-i", str(src), "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(tmp)]
        try:
            cp = subprocess.run(cmd, capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if cp.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(out)
            return True
    tmp.unlink(missing_ok=True)
    return False


def cached_snapshot(ffmpeg: str, cache_dir: Path, cam_id: int, key: str, src: Path) -> Path | None:
    out = cache_dir / f"snap_{cam_id}_{key}.jpg"
    if out.exists():
        return out
    with _lock:
        if out.exists():
            return out
        if not extract_frame(ffmpeg, src, out):
            return None
        for old in cache_dir.glob(f"snap_{cam_id}_*.jpg"):
            if old != out:
                old.unlink(missing_ok=True)
    return out
