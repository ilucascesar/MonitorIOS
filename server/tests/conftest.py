from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _find(name: str) -> str | None:
    env = os.environ.get(f"MONITORIOS_TEST_{name.upper()}")
    if env and Path(env).is_file():
        return env
    found = shutil.which(name)
    if found:
        return found
    pattern = os.path.expandvars(rf"%LOCALAPPDATA%\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\{name}.exe")
    hits = glob.glob(pattern)
    return hits[0] if hits else None


FFMPEG = _find("ffmpeg")
FFPROBE = _find("ffprobe")
needs_ffmpeg = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="FFmpeg/FFprobe não encontrados")


@pytest.fixture
def runtime(tmp_path):
    from app.runtime import Runtime

    rt = Runtime(tmp_path / "data", start_workers=False)
    rt.cfg.update({
        "storage_root": str(tmp_path / "remote" / "MonitorIOS"),
        "smb_share": str(tmp_path / "remote"),
        "ffmpeg_path": FFMPEG or "",
        "ffprobe_path": FFPROBE or "",
    })
    (tmp_path / "remote").mkdir()
    yield rt
    rt.db.close()


@pytest.fixture(scope="session")
def source_video(tmp_path_factory) -> Path:
    """Vídeo H.264 sintético (somente para testes) usado como entrada no lugar do RTSP."""
    if not FFMPEG:
        pytest.skip("FFmpeg não encontrado")
    out = tmp_path_factory.mktemp("src") / "src.ts"
    subprocess.run([
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
        "-t", "20", "-c:v", "libx264", "-preset", "veryfast", "-g", "15", "-pix_fmt", "yuv420p",
        "-f", "mpegts", str(out),
    ], check=True, timeout=120)
    return out
