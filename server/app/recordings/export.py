"""Exportação de trechos: concatena segmentos (sem recodificar) num MP4 para o iPhone.

Roda em fila própria, com prioridade baixa, sem interferir na gravação. Lacunas no
intervalo são registradas no job (o vídeo final "salta" sobre elas).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from ..config import Config
from ..database import Database
from .timeline import gaps, merge_intervals

log = logging.getLogger("recordings.export")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
BELOW_NORMAL_PRIORITY_CLASS = 0x00004000 if sys.platform == "win32" else 0
MAX_EXPORT_SECONDS = 6 * 3600
_TIME_RE = re.compile(r"out_time_us=(\d+)")


def _concat_escape(p: Path) -> str:
    return str(p).replace("\\", "/").replace("'", r"'\''")


class ExportManager:
    def __init__(self, cfg: Config, db: Database, storage, events):
        self.cfg = cfg
        self.db = db
        self.storage = storage
        self.events = events
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._t = threading.Thread(target=self._loop, name="exporter", daemon=True)

    def start(self) -> None:
        # Jobs interrompidos por reinício do serviço são marcados como falhos.
        self.db.execute("UPDATE exports SET status='failed', error='interrompido pelo reinício do servidor' WHERE status IN ('queued','running')")
        self._t.start()

    def stop(self) -> None:
        self._q.put(None)

    def create(self, camera_id: int, user_id: int, start: float, end: float) -> dict:
        if end <= start:
            raise ValueError("horário final deve ser maior que o inicial")
        if end - start > MAX_EXPORT_SECONDS:
            raise ValueError("intervalo máximo de exportação é de 6 horas")
        segs = self.db.segments_in_range(camera_id, start, end)
        if not segs:
            raise LookupError("não há gravação no intervalo escolhido")
        job_id = uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO exports(id, camera_id, user_id, start_ts, end_ts, status, created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, camera_id, user_id, start, end, "queued", time.time()),
        )
        self._q.put(job_id)
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        row = self.db.one("SELECT * FROM exports WHERE id=?", (job_id,))
        if row:
            row["gaps"] = json.loads(row["gaps"] or "[]")
            row.pop("file", None)
        return row

    def file_path(self, job_id: str) -> Path | None:
        row = self.db.one("SELECT file, status FROM exports WHERE id=?", (job_id,))
        if not row or row["status"] != "done" or not row["file"]:
            return None
        p = self.cfg.exports_dir / row["file"]
        return p if p.is_file() else None

    def delete(self, job_id: str) -> None:
        p = self.file_path(job_id)
        if p:
            p.unlink(missing_ok=True)
        self.db.execute("DELETE FROM exports WHERE id=?", (job_id,))

    def cleanup(self, max_age: float = 24 * 3600) -> None:
        old = self.db.query("SELECT id FROM exports WHERE created_at < ?", (time.time() - max_age,))
        for r in old:
            self.delete(r["id"])

    def _loop(self) -> None:
        while True:
            job_id = self._q.get()
            if job_id is None:
                return
            try:
                self._run(job_id)
            except Exception as e:
                log.exception("falha na exportação")
                self.db.execute("UPDATE exports SET status='failed', error=?, finished_at=? WHERE id=?", (str(e)[:300], time.time(), job_id))
            try:
                self.cleanup()
            except Exception:
                pass

    def _start_time(self, path: Path) -> float:
        cp = subprocess.run(
            [self.cfg.ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=start_time",
             "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30, creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL,
        )
        try:
            return float(cp.stdout.strip().splitlines()[0])
        except (ValueError, IndexError):
            return 0.0

    def _run(self, job_id: str) -> None:
        job = self.db.one("SELECT * FROM exports WHERE id=?", (job_id,))
        if not job:
            return
        self.db.execute("UPDATE exports SET status='running' WHERE id=?", (job_id,))
        start, end = job["start_ts"], job["end_ts"]
        segs = self.db.segments_in_range(job["camera_id"], start, end)
        if not segs:
            raise LookupError("não há gravação no intervalo")
        spans = merge_intervals([(s["start_ts"], s["end_ts"]) for s in segs])
        gap_list = [[a, b] for a, b in gaps(spans, start, end)]
        cam = self.db.one("SELECT name FROM cameras WHERE id=?", (job["camera_id"],)) or {"name": "camera"}

        lines = ["ffconcat version 1.0"]
        total = 0.0
        for seg in segs:
            p = self.storage.resolve(seg)
            if not p.exists():
                continue
            lines.append(f"file '{_concat_escape(p)}'")
            inpoint = max(0.0, start - seg["start_ts"])
            outpoint = min(seg["duration"], end - seg["start_ts"])
            trim_in, trim_out = inpoint > 0.05, outpoint < seg["duration"] - 0.05
            if trim_in or trim_out:
                # inpoint/outpoint usam o timestamp absoluto do arquivo (TS não reinicia em 0).
                base = self._start_time(p)
                if trim_in:
                    lines.append(f"inpoint {base + inpoint:.3f}")
                if trim_out:
                    lines.append(f"outpoint {base + outpoint:.3f}")
            total += max(0.0, outpoint - inpoint)
        if total <= 0:
            raise LookupError("arquivos do intervalo não estão acessíveis")
        list_path = self.cfg.exports_dir / f"{job_id}.ffconcat"
        list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", cam["name"])[:32]
        out_name = f"{safe_name}_{time.strftime('%Y%m%d_%H%M%S', time.localtime(start))}_{job_id[:6]}.mp4"
        out = self.cfg.exports_dir / out_name
        hevc = any(s["codec"] in ("hevc", "h265") for s in segs)
        cmd = [
            self.cfg.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostats", "-y",
            "-f", "concat", "-safe", "0", "-i", str(list_path),
            "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "aac", "-b:a", "64k",
            "-fflags", "+genpts", "-avoid_negative_ts", "make_zero",
            "-movflags", "+faststart", "-progress", "pipe:1",
        ]
        if hevc:
            cmd += ["-tag:v", "hvc1"]
        cmd.append(str(out))
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                creationflags=CREATE_NO_WINDOW | BELOW_NORMAL_PRIORITY_CLASS)
        last_update = 0.0
        assert proc.stdout is not None
        for raw in iter(proc.stdout.readline, b""):
            m = _TIME_RE.search(raw.decode("ascii", "ignore"))
            if m and time.time() - last_update > 0.5:
                progress = min(0.99, int(m.group(1)) / 1e6 / total)
                self.db.execute("UPDATE exports SET progress=? WHERE id=?", (round(progress, 3), job_id))
                last_update = time.time()
        rc = proc.wait()
        err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace").strip()
        list_path.unlink(missing_ok=True)
        if rc != 0 or not out.exists():
            out.unlink(missing_ok=True)
            raise RuntimeError(f"ffmpeg falhou ({rc}): {err[-200:]}")
        self.db.execute(
            "UPDATE exports SET status='done', progress=1, file=?, size=?, gaps=?, finished_at=? WHERE id=?",
            (out_name, out.stat().st_size, json.dumps(gap_list), time.time(), job_id),
        )
        log.info("exportação concluída", extra={"ctx": {"job": job_id, "seconds": round(total, 1)}})
