"""Validação de caminhos: nada fora das raízes conhecidas é lido ou apagado."""
from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

MEDIA_EXTENSIONS = {".ts", ".mp4"}
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.\-]+$")
# Pastas criadas pelo Agent DVR (só leitura) podem ter parênteses e espaços, ex.: "volume(sda1)".
_FOREIGN_COMPONENT = re.compile(r"^[A-Za-z0-9_.\-() ]+$")


def safe_join(root: Path | str, rel: str, foreign: bool = False) -> Path:
    """Junta ``rel`` a ``root`` garantindo que o resultado fique dentro de ``root``."""
    if not rel or "\x00" in rel:
        raise ValueError("caminho vazio")
    rel_norm = rel.replace("\\", "/")
    if rel_norm.startswith("/"):
        raise ValueError("caminho absoluto não permitido")
    parts = [p for p in rel_norm.split("/") if p]
    pattern = _FOREIGN_COMPONENT if foreign else _SAFE_COMPONENT
    if any(p in (".", "..") or p != p.strip() or not pattern.match(p) for p in parts):
        raise ValueError("componente de caminho inválido")
    root_abs = os.path.abspath(str(root))
    full = os.path.abspath(os.path.join(root_abs, *parts))
    if os.path.commonpath([os.path.normcase(root_abs), os.path.normcase(full)]) != os.path.normcase(root_abs):
        raise ValueError("caminho fora da raiz")
    return Path(full)


def is_media_file(path: Path) -> bool:
    return path.suffix.lower() in MEDIA_EXTENSIONS


def sanitize_folder(name: str) -> str:
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", ascii_name.strip()).strip("_")
    return (s or "camera")[:48]
