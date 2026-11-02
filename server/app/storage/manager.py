"""Armazenamento: buffer local -> pendrive SMB, saúde, retenção e reconciliação.

Decisão de arquitetura: o FFmpeg grava SEMPRE no disco local (buffer) e um transferidor
copia os segmentos concluídos para o SMB. Gravar direto no SMB faria qualquer oscilação
do Wi-Fi/roteador travar o muxer e perder vídeo; com o buffer, se o pendrive sumir, a
gravação continua localmente e os arquivos são enviados quando ele voltar.

Segurança: só são apagados arquivos que (1) estão no índice, (2) ficam dentro da raiz
``MonitorIOS`` validada com marcador e (3) têm extensão de mídia. Pastas do Agent DVR
nunca são tocadas.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

from ..config import Config
from ..database import Database
from ..recordings.indexer import probe_segment, file_mtime
from ..security import secrets as vault
from ..security.paths import safe_join, is_media_file
from . import smb

log = logging.getLogger("storage")

MARKER = ".monitorios-root"
GB = 1024**3
_NAME_HINT = re.compile(r"_(\d{8}-\d{6})\.(ts|mp4)$", re.I)


class StorageManager:
    def __init__(self, cfg: Config, db: Database, events, active_files: Callable[[], set[Path]] = lambda: set()):
        self.cfg = cfg
        self.db = db
        self.events = events
        self.active_files = active_files
        self.state: dict = {"status": "unknown", "reason": "ainda não verificado", "checked_at": None}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._pending_unlink: set[Path] = set()
        self._last_status = None
        self._wake_uploader = threading.Event()
        self._reconcile_lock = threading.Lock()

    # ------------------------------------------------------------- paths
    @property
    def root(self) -> Path:
        return self.cfg.storage_root

    def resolve(self, seg: dict) -> Path:
        base = self.cfg.buffer_dir if seg["location"] == "local" else self.root
        return safe_join(base, seg["path"])

    @staticmethod
    def remote_rel(folder: str, start_ts: float, filename: str) -> str:
        t = time.localtime(start_ts)
        return f"{folder}/{time.strftime('%Y-%m-%d', t)}/{time.strftime('%H', t)}/{filename}"

    # --------------------------------------------------------- lifecycle
    def start(self) -> None:
        for target, name in (
            (self._health_loop, "storage-health"),
            (self._upload_loop, "storage-upload"),
            (self._maintenance_loop, "storage-maint"),
        ):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._wake_uploader.set()
        for t in self._threads:
            t.join(10)

    # ------------------------------------------------------------ health
    def _smb_password(self) -> str:
        enc = self.db.get_secret("smb_password")
        return vault.decrypt(enc) if enc else ""

    def set_smb_credentials(self, username: str, password: str) -> None:
        self.cfg.update({"smb_username": username})
        self.db.set_secret("smb_password", vault.encrypt(password))

    def _is_unc(self) -> bool:
        return str(self.root).startswith("\\\\")

    def check_health(self) -> dict:
        s = self.cfg.settings
        root = self.root
        st: dict = {"checked_at": time.time(), "root": str(root)}
        try:
            if not os.path.isdir(root):
                if self._is_unc() and s.smb_username:
                    ok, msg = smb.connect(s.smb_share, s.smb_username, self._smb_password())
                    st["smb_connect"] = msg
                # Compartilhamento acessível mas sem a pasta: cria. Sem compartilhamento: indisponível.
                share_ok = os.path.isdir(s.smb_share) if s.smb_share else os.path.isdir(root.parent)
                if share_ok:
                    root.mkdir(parents=True, exist_ok=True)
                elif not os.path.isdir(root):
                    raise OSError(st.get("smb_connect") or "compartilhamento inacessível (roteador desligado ou pendrive desconectado)")
            marker = root / MARKER
            if not marker.exists():
                marker.write_text(json.dumps({"system": "MonitorIOS", "created": time.time()}), encoding="utf-8")
            # Teste real de escrita/leitura.
            probe = root / f".probe-{os.getpid()}"
            payload = os.urandom(64 * 1024)
            t0 = time.perf_counter()
            with open(probe, "wb") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            with open(probe, "rb") as f:
                back = f.read()
            probe.unlink()
            st["write_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            if back != payload:
                raise OSError("verificação de leitura divergente")
            du = shutil.disk_usage(root)
            st.update(total_bytes=du.total, free_bytes=du.free)
            if du.free < s.min_free_gb * GB:
                st.update(status="degraded", reason="pouco espaço livre no pendrive")
            else:
                st.update(status="ok", reason="")
        except OSError as e:
            st.update(status="unavailable", reason=str(getattr(e, "strerror", None) or e)[:200], total_bytes=None, free_bytes=None)
        st["used_by_system_bytes"] = self.db.usage_bytes("remote")
        st["buffer_bytes"] = self.db.usage_bytes("local")
        st["buffer_max_bytes"] = int(s.buffer_max_gb * GB)
        r = self.db.one("SELECT COUNT(*) AS n FROM segments WHERE location='local'")
        st["upload_backlog"] = int(r["n"]) if r else 0
        st["upload_enabled"] = s.upload_enabled
        st["retention_days"] = s.retention_days
        st["max_storage_bytes"] = int(s.max_storage_gb * GB)
        try:
            bdu = shutil.disk_usage(self.cfg.buffer_dir)
            st["buffer_disk_free_bytes"] = bdu.free
        except OSError:
            pass
        if st["status"] != self._last_status:
            if self._last_status is not None or st["status"] != "ok":
                lvl = "info" if st["status"] == "ok" else ("warning" if st["status"] == "degraded" else "error")
                self.events.emit(lvl, "storage", f"armazenamento: {st['status']} {st.get('reason') or ''}".strip())
            self._last_status = st["status"]
            if st["status"] == "ok":
                self._wake_uploader.set()
        self.state = st
        self.events.bump()
        return st

    def _health_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_health()
            except Exception:
                log.exception("erro na verificação do armazenamento")
            self._stop.wait(30 if self.state.get("status") == "ok" else 15)

    # ------------------------------------------------------------ upload
    def _upload_loop(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            self._retry_unlinks()
            s = self.cfg.settings
            if not s.upload_enabled or self.state.get("status") not in ("ok", "degraded"):
                self._wake_uploader.wait(10)
                self._wake_uploader.clear()
                continue
            segs = self.db.query(
                "SELECT s.*, c.folder FROM segments s JOIN cameras c ON c.id=s.camera_id "
                "WHERE s.location='local' AND s.status!='missing' ORDER BY s.start_ts LIMIT 20"
            )
            if not segs:
                self._wake_uploader.wait(5)
                self._wake_uploader.clear()
                continue
            for seg in segs:
                if self._stop.is_set():
                    return
                try:
                    self.upload_segment(seg)
                    backoff = 5.0
                except OSError as e:
                    log.warning("falha ao transferir segmento", extra={"ctx": {"segment": seg["id"], "error": str(e)}})
                    self.state["status"] = "degraded"
                    self.state["reason"] = f"falha na transferência: {e}"[:200]
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 120)
                    break

    def upload_segment(self, seg: dict) -> None:
        src = safe_join(self.cfg.buffer_dir, seg["path"])
        if not src.exists():
            self.db.set_segment_status(seg["id"], "missing")
            self.events.emit("error", "storage", f"segmento sumiu do buffer antes do envio: {seg['path']}", seg["camera_id"])
            return
        rel = self.remote_rel(seg["folder"], seg["start_ts"], src.name)
        dest = safe_join(self.root, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with open(src, "rb") as fi, open(tmp, "wb") as fo:
            shutil.copyfileobj(fi, fo, 1024 * 1024)
            fo.flush()
            os.fsync(fo.fileno())
        if tmp.stat().st_size != src.stat().st_size:
            tmp.unlink(missing_ok=True)
            raise OSError("tamanho divergente após cópia")
        os.replace(tmp, dest)
        self.db.mark_segment_remote(seg["id"], rel)
        try:
            src.unlink()
        except OSError:
            # Arquivo aberto por uma reprodução em andamento: apaga depois.
            self._pending_unlink.add(src)

    def _retry_unlinks(self) -> None:
        for p in list(self._pending_unlink):
            try:
                p.unlink(missing_ok=True)
                self._pending_unlink.discard(p)
            except OSError:
                pass

    # ---------------------------------------------------------- retention
    def _maintenance_loop(self) -> None:
        last_reconcile = 0.0
        self._stop.wait(20)
        while not self._stop.is_set():
            try:
                self.enforce_buffer_limit()
                self.run_retention()
                if time.time() - last_reconcile > 6 * 3600 and self.state.get("status") == "ok":
                    self.reconcile_remote()
                    last_reconcile = time.time()
                self.db.prune_events(time.time() - 90 * 86400)
            except Exception:
                log.exception("erro na manutenção do armazenamento")
            self._stop.wait(600)

    def _delete_segment(self, seg: dict) -> bool:
        try:
            p = self.resolve(seg)
        except ValueError:
            log.error("caminho inválido no índice; ignorado", extra={"ctx": {"segment": seg["id"]}})
            return False
        if not is_media_file(p):
            return False
        if seg["location"] == "remote" and not (self.root / MARKER).exists():
            # Sem marcador não temos certeza de que a raiz é nossa: não apaga nada.
            return False
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            log.warning("não foi possível apagar segmento", extra={"ctx": {"segment": seg["id"], "error": str(e)}})
            return False
        self.db.delete_segment(seg["id"])
        self._prune_empty_dirs(p.parent)
        return True

    def _prune_empty_dirs(self, d: Path) -> None:
        base = {os.path.normcase(os.path.abspath(self.root)), os.path.normcase(os.path.abspath(self.cfg.buffer_dir))}
        for _ in range(2):  # hora/ e data/
            try:
                if os.path.normcase(os.path.abspath(d.parent)) in base:
                    return
                d.rmdir()
            except OSError:
                return
            d = d.parent

    def run_retention(self) -> dict:
        s = self.cfg.settings
        deleted = {"age": 0, "quota": 0, "free_space": 0}
        cutoff = time.time() - s.retention_days * 86400
        while True:
            old = self.db.query("SELECT * FROM segments WHERE end_ts < ? AND protected=0 ORDER BY start_ts LIMIT 500", (cutoff,))
            if not old:
                break
            n = sum(self._delete_segment(seg) for seg in old)
            deleted["age"] += n
            if n == 0:
                break
        cap = s.max_storage_gb * GB
        total = self.db.usage_bytes()
        while total > cap:
            batch = self.db.query("SELECT * FROM segments WHERE protected=0 ORDER BY start_ts LIMIT 100")
            if not batch:
                self.events.emit("warning", "retention", "limite global excedido apenas com gravações protegidas")
                break
            freed = 0
            for seg in batch:
                if self._delete_segment(seg):
                    freed += seg["size"]
                    deleted["quota"] += 1
                    if total - freed <= cap:
                        break
            if not freed:
                break
            total -= freed
        if self.state.get("status") in ("ok", "degraded") and self.state.get("free_bytes") is not None:
            free = self.state["free_bytes"]
            target = s.min_free_gb * GB * 1.2
            guard = 0
            while free < target and guard < 20:
                guard += 1
                batch = self.db.query("SELECT * FROM segments WHERE location='remote' AND protected=0 ORDER BY start_ts LIMIT 50")
                if not batch:
                    break
                for seg in batch:
                    if self._delete_segment(seg):
                        free += seg["size"]
                        deleted["free_space"] += 1
        if any(deleted.values()):
            self.events.emit("info", "retention", f"retenção: {deleted['age']} por idade, {deleted['quota']} por limite global, {deleted['free_space']} por espaço livre")
        return deleted

    def enforce_buffer_limit(self) -> int:
        cap = self.cfg.settings.buffer_max_gb * GB
        used = self.db.usage_bytes("local")
        if used <= cap:
            return 0
        dropped = 0
        while used > cap * 0.9:
            batch = self.db.query("SELECT * FROM segments WHERE location='local' AND protected=0 ORDER BY start_ts LIMIT 50")
            if not batch:
                break
            for seg in batch:
                if self._delete_segment(seg):
                    used -= seg["size"]
                    dropped += 1
                if used <= cap * 0.9:
                    break
        if dropped:
            self.events.emit("error", "storage", f"buffer local cheio com o pendrive indisponível: {dropped} segmento(s) mais antigo(s) descartado(s)")
        return dropped

    # --------------------------------------------------------- reconcile
    def request_reconcile(self) -> None:
        """Reconcilia em segundo plano, por exemplo quando câmeras recém-cadastradas já têm vídeos
        no pendrive (reinstalação ou índice perdido). Ignora o pedido se já houver uma em andamento."""
        if self._reconcile_lock.locked():
            return

        def run() -> None:
            try:
                if self.state.get("status") != "ok":
                    self.check_health()
                if self.state.get("status") == "ok":
                    self.reconcile_remote()
            except Exception:
                log.exception("erro na reconciliação")

        threading.Thread(target=run, name="storage-reconcile", daemon=True).start()

    def reconcile_remote(self, limit: int = 5000) -> dict:
        """Indexa vídeos do sistema que existem no SMB mas não no banco, e marca ausentes."""
        with self._reconcile_lock:
            return self._reconcile_remote(limit)

    def _reconcile_remote(self, limit: int) -> dict:
        root = self.root
        result = {"added": 0, "missing": 0}
        if not (root / MARKER).exists():
            return result
        cams = {c["folder"]: c for c in self.db.list_cameras(include_deleted=True)}
        known = self.db.known_paths("remote")
        seen: set[str] = set()
        for folder, cam in cams.items():
            cam_dir = root / folder
            if not cam_dir.is_dir():
                continue
            for dirpath, _dirs, files in os.walk(cam_dir):
                for name in files:
                    p = Path(dirpath) / name
                    if not is_media_file(p) or name.endswith(".part"):
                        continue
                    rel = p.relative_to(root).as_posix()
                    seen.add(rel)
                    if rel in known or result["added"] >= limit:
                        continue
                    # Arquivo recém-enviado: o uploader ainda vai registrá-lo no banco.
                    if time.time() - file_mtime(p) < 120:
                        continue
                    res = probe_segment(self.cfg.ffprobe, p, self.cfg.settings.hls_part_seconds)
                    if not res.ok:
                        continue
                    start = self._start_from_name(name)
                    if start is None:
                        start = file_mtime(p) - res.duration
                    self.db.insert_segment({
                        "camera_id": cam["id"], "run_id": name.split("_", 1)[0], "location": "remote", "path": rel,
                        "container": res.container, "start_ts": start, "end_ts": start + res.duration,
                        "duration": res.duration, "size": res.size, "codec": res.codec, "width": res.width,
                        "height": res.height, "parts": res.parts, "init_len": res.init_len, "status": "reconciled",
                    })
                    result["added"] += 1
        for rel in known - seen:
            row = self.db.one("SELECT id, status FROM segments WHERE location='remote' AND path=?", (rel,))
            if row and row["status"] != "missing":
                self.db.set_segment_status(row["id"], "missing")
                result["missing"] += 1
        if result["added"] or result["missing"]:
            self.events.emit("info" if not result["missing"] else "warning", "reconcile",
                             f"reconciliação: {result['added']} adicionado(s), {result['missing']} ausente(s) no pendrive")
        return result

    @staticmethod
    def _start_from_name(name: str) -> float | None:
        """Dica de horário no nome (usada só quando o índice foi perdido)."""
        m = _NAME_HINT.search(name)
        if not m:
            return None
        try:
            return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").timestamp()
        except ValueError:
            return None
