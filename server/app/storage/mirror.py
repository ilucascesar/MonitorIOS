"""Cópia local das gravações no disco do PC, para a reprodução não depender do pendrive.

O compartilhamento do roteador leva de 0,1 s a mais de 10 s para abrir um arquivo enquanto as
câmeras gravam nele, e o AVPlayer abre os arquivos a cada trecho pedido. Por isso:

- segmentos novos do MonitorIOS ficam no disco local depois de copiados para o pendrive
  (o arquivo do buffer é movido para cá em vez de apagado);
- segmentos antigos são copiados quando alguém os assiste, e os próximos minutos da mesma
  câmera vêm em segundo plano, antes de serem pedidos;
- gravações do Agent DVR (somente leitura) também são copiadas em segundo plano; o original
  no pendrive nunca é alterado.

O pendrive continua sendo o armazenamento oficial: a cópia local tem tamanho máximo
(``local_cache_gb``), segue a retenção e pode ser apagada a qualquer momento.
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path

from ..security.paths import safe_join

log = logging.getLogger("storage.mirror")

GB = 1024 ** 3
AGENTDVR_DIR = "_agentdvr"
PREFETCH_AHEAD = 5          # segmentos seguintes copiados enquanto alguém assiste
WARM_HOURS = 48             # gravações recentes copiadas proativamente
FETCH_TIMEOUT = 60


class LocalMirror:
    def __init__(self, cfg, db, source_path, storage_ok=lambda: True):
        self.cfg = cfg
        self.db = db
        self.source_path = source_path      # seg -> caminho no pendrive
        self.storage_ok = storage_ok
        self._queue: OrderedDict[int, dict] = OrderedDict()
        self._watched: OrderedDict[int, float] = OrderedDict()
        self._qlock = threading.Lock()
        self._wake = threading.Event()
        self._inflight: dict[Path, threading.Event] = {}
        self._ilock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.stats = {"hits": 0, "fetched": 0, "fetch_seconds": 0.0, "bytes": 0, "files": 0}

    # ------------------------------------------------------------ caminhos
    @property
    def enabled(self) -> bool:
        return self.cfg.settings.local_cache_gb > 0

    @property
    def root(self) -> Path:
        return self.cfg.mirror_dir

    def path_for(self, seg: dict) -> Path | None:
        if seg["location"] == "remote":
            return safe_join(self.root, seg["path"])
        if seg["location"] == "agentdvr":
            if not self.agentdvr_remote():
                return None  # Agent DVR já grava no disco do PC: copiar só duplicaria
            return safe_join(self.root / AGENTDVR_DIR, seg["path"], foreign=True)
        return None

    def agentdvr_remote(self) -> bool:
        return str(self.cfg.agentdvr_root).startswith("\\\\")

    def lookup(self, seg: dict) -> Path | None:
        if not self.enabled:
            return None
        try:
            p = self.path_for(seg)
        except ValueError:
            return None
        if p is not None and p.is_file() and p.stat().st_size == seg["size"]:
            self.stats["hits"] += 1
            return p
        return None

    # --------------------------------------------------------- operações
    def adopt(self, src: Path, rel: str) -> bool:
        """Segmento recém-enviado ao pendrive: o arquivo do buffer passa a ser a cópia local."""
        if not self.enabled:
            return False
        try:
            dest = safe_join(self.root, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            os.replace(src, dest)
            return True
        except (OSError, ValueError):
            return False

    def remove(self, seg: dict) -> None:
        try:
            p = self.path_for(seg)
            if p is not None:
                p.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass

    def ensure(self, seg: dict) -> Path | None:
        """Cópia local do segmento, copiando agora se preciso (só segmentos do MonitorIOS, que são
        pequenos; os do Agent DVR, de até 15 min, vão para a fila em segundo plano)."""
        if not self.enabled or seg["location"] not in ("remote", "agentdvr"):
            return None
        hit = self.lookup(seg)
        if hit:
            return hit
        if seg["location"] == "agentdvr" or not self.storage_ok():
            self.enqueue([seg], front=True)
            return None
        return self._fetch(seg)

    def _fetch(self, seg: dict) -> Path | None:
        try:
            dest = self.path_for(seg)
            src = self.source_path(seg)
        except ValueError:
            return None
        if dest is None:
            return None
        with self._ilock:
            ev = self._inflight.get(dest)
            owner = ev is None
            if owner:
                ev = self._inflight[dest] = threading.Event()
        if not owner:
            ev.wait(FETCH_TIMEOUT)
            return dest if dest.is_file() else None
        t0 = time.time()
        tmp = dest.with_name(dest.name + ".part")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, tmp)
            shutil.copystat(src, tmp)
            if tmp.stat().st_size != seg["size"]:
                raise OSError("tamanho divergente")
            os.replace(tmp, dest)
            self.stats["fetched"] += 1
            self.stats["fetch_seconds"] += time.time() - t0
            return dest
        except OSError as e:
            tmp.unlink(missing_ok=True)
            log.debug("cópia local falhou", extra={"ctx": {"segment": seg["id"], "error": str(e)}})
            return None
        finally:
            with self._ilock:
                self._inflight.pop(dest, None)
            ev.set()

    def enqueue(self, segs: list[dict], front: bool = False) -> None:
        if not self.enabled:
            return
        with self._qlock:
            for seg in reversed(segs) if front else segs:
                if seg["id"] in self._queue:
                    if front:
                        self._queue.move_to_end(seg["id"], last=False)
                    continue
                self._queue[seg["id"]] = seg
                if front:
                    self._queue.move_to_end(seg["id"], last=False)
        self._wake.set()

    def prefetch_after(self, seg: dict) -> None:
        """Alguém está assistindo ``seg``: adianta a cópia dos próximos minutos da câmera
        (uma vez por segmento; o player pede vários trechos do mesmo arquivo)."""
        if not self.enabled:
            return
        with self._qlock:
            if seg["id"] in self._watched:
                return
            self._watched[seg["id"]] = time.time()
            while len(self._watched) > 512:
                self._watched.popitem(last=False)
        nxt = self.db.query(
            "SELECT * FROM segments WHERE camera_id=? AND start_ts>? AND location IN ('remote','agentdvr') "
            "ORDER BY start_ts LIMIT ?", (seg["camera_id"], seg["start_ts"], PREFETCH_AHEAD),
        )
        todo = [s for s in nxt if not self.lookup(s)]
        if seg["location"] == "agentdvr" and not self.lookup(seg):
            todo.insert(0, seg)
        if todo:
            self.enqueue(todo, front=True)

    # ------------------------------------------------------- segundo plano
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="storage-mirror", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _next(self) -> dict | None:
        with self._qlock:
            if not self._queue:
                return None
            _id, seg = self._queue.popitem(last=False)
            return seg

    def _loop(self) -> None:
        last_warm = last_trim = 0.0
        self._stop.wait(15)
        while not self._stop.is_set():
            try:
                if not self.enabled:
                    self._stop.wait(60)
                    continue
                if time.time() - last_trim > 600:
                    self.trim()
                    last_trim = time.time()
                seg = self._next()
                if seg is not None:
                    if self.storage_ok() and not self.lookup(seg):
                        self._fetch(seg)
                    continue
                if time.time() - last_warm > 900 and self.storage_ok():
                    self.warm()
                    last_warm = time.time()
                    continue
                self._wake.wait(30)
                self._wake.clear()
            except Exception:
                log.exception("erro na cópia local das gravações")
                self._stop.wait(30)

    def warm(self, limit: int = 300) -> int:
        """Copia as gravações recentes que ainda só estão no pendrive (mais novas primeiro)."""
        since = time.time() - WARM_HOURS * 3600
        rows = self.db.query(
            "SELECT * FROM segments WHERE location='remote' AND end_ts>? AND status!='missing' "
            "ORDER BY start_ts DESC LIMIT ?", (since, limit * 3),
        )
        todo = [r for r in rows if not self.lookup(r)][:limit]
        self.enqueue(todo)
        return len(todo)

    def trim(self) -> dict:
        """Mantém a cópia dentro de ``local_cache_gb`` e da retenção (apaga as mais antigas)."""
        cap = self.cfg.settings.local_cache_gb * GB
        cutoff = time.time() - (self.cfg.settings.retention_days + 1) * 86400
        files: list[tuple[float, int, str]] = []
        total = removed = 0
        if not self.root.is_dir():
            return {"bytes": 0, "files": 0, "removed": 0}
        for dirpath, _dirs, names in os.walk(self.root):
            for name in names:
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if name.endswith(".part") and time.time() - st.st_mtime > 3600:
                    os.remove(p)
                    continue
                if st.st_mtime < cutoff:
                    try:
                        os.remove(p)
                        removed += 1
                    except OSError:
                        pass
                    continue
                files.append((st.st_mtime, st.st_size, p))
                total += st.st_size
        if total > cap:
            files.sort()
            target = cap * 0.9
            for _m, size, p in files:
                if total <= target:
                    break
                try:
                    os.remove(p)
                    total -= size
                    removed += 1
                except OSError:
                    pass
        self.stats["bytes"] = total
        self.stats["files"] = len(files) - removed if removed <= len(files) else 0
        if removed:
            log.info("cópia local reduzida", extra={"ctx": {"removed": removed, "bytes": total}})
        return {"bytes": total, "files": self.stats["files"], "removed": removed}
