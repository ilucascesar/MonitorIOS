"""Índice SQLite (fica no disco local do servidor, nunca no pendrive SMB).

Todos os horários são epoch UTC em segundos (float). A conversão para fuso local é feita
somente na borda da API.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS cameras (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    host TEXT NOT NULL,
    onvif_port INTEGER NOT NULL DEFAULT 80,
    rtsp_port INTEGER NOT NULL DEFAULT 554,
    username TEXT NOT NULL DEFAULT '',
    password_enc TEXT NOT NULL DEFAULT '',
    main_url TEXT NOT NULL DEFAULT '',
    sub_url TEXT NOT NULL DEFAULT '',
    record_stream TEXT NOT NULL DEFAULT 'main',
    audio_mode TEXT NOT NULL DEFAULT 'none',
    record_enabled INTEGER NOT NULL DEFAULT 1,
    live_enabled INTEGER NOT NULL DEFAULT 1,
    folder TEXT NOT NULL UNIQUE,
    codec TEXT NOT NULL DEFAULT '',
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    fps REAL NOT NULL DEFAULT 0,
    sub_codec TEXT NOT NULL DEFAULT '',
    sub_width INTEGER NOT NULL DEFAULT 0,
    sub_height INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0,
    mac TEXT NOT NULL DEFAULT '',
    deleted INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    camera_id INTEGER NOT NULL,
    run_id TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL,              -- 'local' (buffer) | 'remote' (SMB) | 'agentdvr' (só leitura)
    path TEXT NOT NULL,                  -- relativo à raiz do local
    container TEXT NOT NULL DEFAULT 'ts',
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    duration REAL NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    codec TEXT NOT NULL DEFAULT '',
    width INTEGER NOT NULL DEFAULT 0,
    height INTEGER NOT NULL DEFAULT 0,
    parts TEXT NOT NULL DEFAULT '[]',    -- [[offset, length, duration], ...] para HLS byte-range
    init_len INTEGER NOT NULL DEFAULT 0, -- fMP4: bytes de ftyp+moov
    status TEXT NOT NULL DEFAULT 'ok',   -- ok | partial | reconciled | corrupt | missing
    protected INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(location, path)
);
CREATE INDEX IF NOT EXISTS ix_segments_cam_start ON segments(camera_id, start_ts);
CREATE INDEX IF NOT EXISTS ix_segments_location ON segments(location, start_ts);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'viewer',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_used REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    camera_id INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    camera_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    start_ts REAL NOT NULL,
    end_ts REAL NOT NULL,
    status TEXT NOT NULL,       -- queued | running | done | failed
    progress REAL NOT NULL DEFAULT 0,
    file TEXT NOT NULL DEFAULT '',
    size INTEGER NOT NULL DEFAULT 0,
    gaps TEXT NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    finished_at REAL
);

CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

CAMERA_FIELDS = (
    "name", "host", "onvif_port", "rtsp_port", "username", "password_enc", "main_url", "sub_url",
    "record_stream", "audio_mode", "record_enabled", "live_enabled", "folder", "codec", "width",
    "height", "fps", "sub_codec", "sub_width", "sub_height", "sort_order", "mac",
)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            c = self._conn
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.executescript(SCHEMA)
            # Bancos criados antes da coluna mac (endereço da câmera para seguir trocas de IP).
            if "mac" not in {r["name"] for r in c.execute("PRAGMA table_info(cameras)")}:
                c.execute("ALTER TABLE cameras ADD COLUMN mac TEXT NOT NULL DEFAULT ''")
            c.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('schema_version', ?)", (str(SCHEMA_VERSION),))

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------- primitives
    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        with self._lock:
            r = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(r) if r else None

    def transaction(self):
        return _Tx(self)

    # ---------------------------------------------------------------- meta
    def get_secret(self, name: str) -> str | None:
        r = self.one("SELECT value FROM secrets WHERE name=?", (name,))
        return r["value"] if r else None

    def set_secret(self, name: str, value: str) -> None:
        self.execute("INSERT INTO secrets(name, value) VALUES(?, ?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, value))

    # ------------------------------------------------------------- cameras
    def list_cameras(self, include_deleted: bool = False) -> list[dict]:
        where = "" if include_deleted else "WHERE deleted=0"
        return self.query(f"SELECT * FROM cameras {where} ORDER BY sort_order, id")

    def get_camera(self, cam_id: int) -> dict | None:
        return self.one("SELECT * FROM cameras WHERE id=? AND deleted=0", (cam_id,))

    def insert_camera(self, data: dict) -> int:
        now = time.time()
        cols = [k for k in CAMERA_FIELDS if k in data]
        sql = f"INSERT INTO cameras({', '.join(cols)}, created_at, updated_at) VALUES({', '.join('?' * len(cols))}, ?, ?)"
        cur = self.execute(sql, [data[k] for k in cols] + [now, now])
        return int(cur.lastrowid)

    def update_camera(self, cam_id: int, data: dict) -> None:
        cols = [k for k in CAMERA_FIELDS if k in data]
        if not cols:
            return
        sql = f"UPDATE cameras SET {', '.join(f'{k}=?' for k in cols)}, updated_at=? WHERE id=?"
        self.execute(sql, [data[k] for k in cols] + [time.time(), cam_id])

    def soft_delete_camera(self, cam_id: int) -> None:
        # O nome da pasta continua reservado para que gravações antigas não se misturem.
        self.execute("UPDATE cameras SET deleted=1, record_enabled=0, live_enabled=0, updated_at=? WHERE id=?", (time.time(), cam_id))

    # ------------------------------------------------------------ segments
    def insert_segment(self, seg: dict) -> int:
        cur = self.execute(
            """INSERT INTO segments(camera_id, run_id, location, path, container, start_ts, end_ts, duration, size,
                                    codec, width, height, parts, init_len, status, protected, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                seg["camera_id"], seg.get("run_id", ""), seg["location"], seg["path"], seg.get("container", "ts"),
                seg["start_ts"], seg["end_ts"], seg["duration"], seg.get("size", 0), seg.get("codec", ""),
                seg.get("width", 0), seg.get("height", 0), json.dumps(seg.get("parts", [])),
                seg.get("init_len", 0), seg.get("status", "ok"), int(seg.get("protected", 0)), time.time(),
            ),
        )
        return int(cur.lastrowid)

    def get_segment(self, seg_id: int) -> dict | None:
        return self.one("SELECT * FROM segments WHERE id=?", (seg_id,))

    def segments_in_range(self, cam_id: int, start: float, end: float, playable_only: bool = True) -> list[dict]:
        status = "AND status IN ('ok','partial','reconciled')" if playable_only else ""
        return self.query(
            f"""SELECT * FROM segments WHERE camera_id=? AND end_ts > ? AND start_ts < ? {status}
                ORDER BY start_ts, id""",
            (cam_id, start, end),
        )

    def first_segment_after(self, cam_id: int, t: float) -> dict | None:
        return self.one(
            """SELECT * FROM segments WHERE camera_id=? AND start_ts >= ? AND status IN ('ok','partial','reconciled')
               ORDER BY start_ts LIMIT 1""",
            (cam_id, t),
        )

    def last_segment_before(self, cam_id: int, t: float) -> dict | None:
        return self.one(
            """SELECT * FROM segments WHERE camera_id=? AND end_ts <= ? AND status IN ('ok','partial','reconciled')
               ORDER BY end_ts DESC LIMIT 1""",
            (cam_id, t),
        )

    def latest_segment(self, cam_id: int) -> dict | None:
        return self.one(
            # Só gravação própria: o Agent DVR gravando não pode mascarar uma falha do MonitorIOS.
            """SELECT * FROM segments WHERE camera_id=? AND status IN ('ok','partial','reconciled')
               AND location!='agentdvr' ORDER BY end_ts DESC LIMIT 1""",
            (cam_id,),
        )

    def segment_starts(self, cam_id: int, start: float, end: float) -> list[tuple[float, float]]:
        rows = self.query(
            """SELECT start_ts, end_ts FROM segments WHERE camera_id=? AND end_ts > ? AND start_ts < ?
               AND status IN ('ok','partial','reconciled') ORDER BY start_ts""",
            (cam_id, start, end),
        )
        return [(r["start_ts"], r["end_ts"]) for r in rows]

    def known_paths(self, location: str) -> set[str]:
        return {r["path"] for r in self.query("SELECT path FROM segments WHERE location=?", (location,))}

    def mark_segment_remote(self, seg_id: int, rel_path: str) -> None:
        self.execute("UPDATE segments SET location='remote', path=? WHERE id=?", (rel_path, seg_id))

    def set_segment_status(self, seg_id: int, status: str) -> None:
        self.execute("UPDATE segments SET status=? WHERE id=?", (status, seg_id))

    def delete_segment(self, seg_id: int) -> None:
        self.execute("DELETE FROM segments WHERE id=?", (seg_id,))

    def set_protected(self, cam_id: int, start: float, end: float, protected: bool) -> int:
        cur = self.execute(
            "UPDATE segments SET protected=? WHERE camera_id=? AND end_ts > ? AND start_ts < ?",
            (int(protected), cam_id, start, end),
        )
        return cur.rowcount

    def usage_bytes(self, location: str | None = None) -> int:
        if location:
            r = self.one("SELECT COALESCE(SUM(size),0) AS s FROM segments WHERE location=?", (location,))
        else:
            # Arquivos do Agent DVR não contam na cota do MonitorIOS (e nunca são apagados por ela).
            r = self.one("SELECT COALESCE(SUM(size),0) AS s FROM segments WHERE location!='agentdvr'")
        return int(r["s"]) if r else 0

    def usage_by_camera(self) -> list[dict]:
        return self.query(
            """SELECT camera_id, COUNT(*) AS segments, COALESCE(SUM(size),0) AS bytes,
                      MIN(start_ts) AS oldest, MAX(end_ts) AS newest
               FROM segments WHERE location!='agentdvr' GROUP BY camera_id"""
        )

    # --------------------------------------------------------------- events
    def add_event(self, level: str, kind: str, message: str, camera_id: int | None = None) -> None:
        self.execute(
            "INSERT INTO events(ts, level, kind, camera_id, message) VALUES(?,?,?,?,?)",
            (time.time(), level, kind, camera_id, message),
        )

    def recent_events(self, limit: int = 200) -> list[dict]:
        return self.query("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))

    def prune_events(self, older_than: float) -> None:
        self.execute("DELETE FROM events WHERE ts < ?", (older_than,))


class _Tx:
    def __init__(self, db: Database):
        self.db = db

    def __enter__(self):
        self.db._lock.acquire()
        self.db._conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        try:
            self.db._conn.execute("COMMIT" if exc_type is None else "ROLLBACK")
        finally:
            self.db._lock.release()
        return False
