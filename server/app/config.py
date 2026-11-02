"""Configuração do servidor.

Valores não secretos ficam em ``<DATA_DIR>/settings.json``. Segredos (senhas de câmera,
senha SMB, chaves de assinatura) nunca são gravados aqui: ficam no SQLite cifrados com
DPAPI (ver ``app.security.secrets``).
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator


def default_data_dir() -> Path:
    env = os.environ.get("MONITORIOS_DATA")
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "MonitorIOS"
    return Path.home() / ".monitorios"


class Settings(BaseModel):
    # Rede / HTTP
    http_host: str = "0.0.0.0"
    http_port: int = Field(8765, ge=1, le=65535)
    tls_certfile: str = ""
    tls_keyfile: str = ""
    # Painel administrativo e API de administração só pelo próprio PC, a menos que liberado.
    admin_remote_access: bool = False
    session_ttl_hours: int = Field(24 * 30, ge=1)
    media_token_ttl_minutes: int = Field(360, ge=5)

    # Armazenamento
    storage_root: str = r"\\192.168.1.1\volume(sda1)\Gravacoes_Cameras\MonitorIOS"
    smb_share: str = r"\\192.168.1.1\volume(sda1)"
    smb_username: str = ""
    buffer_dir: str = ""
    buffer_max_gb: float = Field(20.0, gt=0)
    upload_enabled: bool = True
    retention_days: int = Field(30, ge=1)
    # Pendrive atual: 32 GB, compartilhado com o Agent DVR durante a migração.
    max_storage_gb: float = Field(18.0, gt=0)
    min_free_gb: float = Field(2.0, ge=0)
    # Gravações antigas do Agent DVR na linha do tempo (somente leitura: nunca movidas nem apagadas).
    agentdvr_playback: bool = True
    agentdvr_media_dir: str = ""  # vazio = pasta "video" ao lado da raiz do MonitorIOS
    agentdvr_objects: str = r"C:\Program Files\Agent\Media\XML\objects.json"

    # Gravação
    segment_seconds: int = Field(60, ge=10, le=900)
    hls_part_seconds: float = Field(4.0, ge=2, le=60)
    stall_timeout_seconds: int = Field(30, ge=10)
    ffmpeg_path: str = ""
    ffprobe_path: str = ""

    # Cópia local das gravações no disco do PC (o pendrive do roteador demora segundos para abrir
    # arquivos enquanto grava). 0 desliga: tudo passa a ser lido do pendrive.
    local_cache_gb: float = Field(40.0, ge=0)

    # Ao vivo
    live_idle_timeout_seconds: int = Field(45, ge=10)
    live_max_processes: int = Field(8, ge=1)
    live_hls_time: int = Field(2, ge=1, le=10)
    # Mantém a conexão com cada câmera aberta: o app vê a imagem na hora, sem esperar o RTSP.
    live_always_on: bool = True

    # Versão do formato do settings.json (migrações de padrões antigos).
    schema_version: int = 1

    log_level: str = "INFO"

    @field_validator("log_level")
    @classmethod
    def _lvl(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("log_level inválido")
        return v


SCHEMA_VERSION = 2


def _migrate(raw: dict) -> bool:
    """Atualiza padrões antigos que o usuário não alterou. Devolve True se mudou algo."""
    version = int(raw.get("schema_version") or 1)
    if version >= SCHEMA_VERSION:
        return False
    if version < 2:
        # Partes de 4 s: o app começa a tocar e pula na linha do tempo baixando bem menos.
        if raw.get("hls_part_seconds") == 10.0:
            raw["hls_part_seconds"] = 4.0
    raw["schema_version"] = SCHEMA_VERSION
    return True


class Config:
    """Settings + caminhos derivados, com persistência atômica e recarga em tempo de execução."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = Path(data_dir or default_data_dir())
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings_path = self.data_dir / "settings.json"
        self._lock = threading.RLock()
        self.settings = self._load()
        for d in (self.logs_dir, self.live_dir, self.exports_dir, self.cache_dir, self.buffer_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    @property
    def db_path(self) -> Path:
        return self.data_dir / "monitor.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def live_dir(self) -> Path:
        return self.data_dir / "live"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def mirror_dir(self) -> Path:
        """Cópia local das gravações (mesmos caminhos relativos do pendrive)."""
        return self.data_dir / "mirror"

    @property
    def buffer_dir(self) -> Path:
        s = self.settings.buffer_dir
        return Path(s) if s else self.data_dir / "buffer"

    @property
    def storage_root(self) -> Path:
        return Path(self.settings.storage_root)

    @property
    def agentdvr_root(self) -> Path:
        s = self.settings.agentdvr_media_dir
        return Path(s) if s else self.storage_root.parent / "video"

    # ------------------------------------------------------------ ffmpeg
    def _find_tool(self, configured: str, name: str) -> str:
        if configured and Path(configured).is_file():
            return configured
        local = self.data_dir / "bin" / f"{name}.exe"
        if local.is_file():
            return str(local)
        found = shutil.which(name)
        if found:
            return found
        return configured or name

    @property
    def ffmpeg(self) -> str:
        return self._find_tool(self.settings.ffmpeg_path, "ffmpeg")

    @property
    def ffprobe(self) -> str:
        return self._find_tool(self.settings.ffprobe_path, "ffprobe")

    # ------------------------------------------------------------ persistence
    def _load(self) -> Settings:
        if self.settings_path.is_file():
            raw = json.loads(self.settings_path.read_text(encoding="utf-8-sig"))
            if _migrate(raw):
                s = Settings.model_validate(raw)
                self._write(s)
                return s
            return Settings.model_validate(raw)
        s = Settings(schema_version=SCHEMA_VERSION)
        self._write(s)
        return s

    def _write(self, s: Settings) -> None:
        tmp = self.settings_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(s.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.settings_path)

    def update(self, changes: dict[str, Any]) -> Settings:
        with self._lock:
            merged = self.settings.model_dump()
            unknown = set(changes) - set(merged)
            if unknown:
                raise ValueError(f"campos desconhecidos: {sorted(unknown)}")
            merged.update(changes)
            new = Settings.model_validate(merged)
            self._write(new)
            self.settings = new
            return new
