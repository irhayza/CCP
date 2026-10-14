"""Penyimpanan SQLite: job bertahan setelah server restart, plus cache hasil analisis."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

TERMINAL = ("done", "failed", "cancelled", "interrupted")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, batch_id TEXT, status TEXT NOT NULL,
  stage TEXT DEFAULT '', progress REAL DEFAULT 0, message TEXT DEFAULT '',
  params TEXT DEFAULT '{}', result TEXT, error TEXT, created REAL, updated REAL
);
CREATE INDEX IF NOT EXISTS idx_jobs_batch ON jobs(batch_id);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, value TEXT NOT NULL, created REAL NOT NULL);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:16]


class Store:
    def __init__(self, path: str | Path):
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            # job yang masih berjalan saat server mati tidak mungkin lanjut
            self._db.execute("UPDATE jobs SET status='interrupted', message='Server dimulai ulang' "
                             "WHERE status IN ('queued','running')")
            self._db.commit()

    # ------------------------------------------------------------------ jobs
    def create_job(self, kind: str, params: dict, batch_id: str | None = None) -> str:
        jid, now = new_id(), time.time()
        with self._lock:
            self._db.execute("INSERT INTO jobs(id,kind,batch_id,status,params,created,updated) VALUES(?,?,?,?,?,?,?)",
                             (jid, kind, batch_id, "queued", json.dumps(params), now, now))
            self._db.commit()
        return jid

    def update_job(self, jid: str, **fields: Any) -> None:
        if not fields:
            return
        for k in ("result", "params"):
            if k in fields and not isinstance(fields[k], str) and fields[k] is not None:
                fields[k] = json.dumps(fields[k])
        fields["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), jid))
            self._db.commit()

    @staticmethod
    def _row(r: sqlite3.Row | None) -> dict | None:
        if r is None:
            return None
        d = dict(r)
        for k in ("params", "result"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        return d

    def get_job(self, jid: str) -> dict | None:
        with self._lock:
            return self._row(self._db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())

    def batch_jobs(self, batch_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs WHERE batch_id=? ORDER BY created, rowid", (batch_id,)).fetchall()
        return [self._row(r) for r in rows]  # type: ignore[misc]

    def count_active(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]

    def purge_jobs(self, older_than: float) -> int:
        with self._lock:
            cur = self._db.execute("DELETE FROM jobs WHERE updated<? AND status IN ('done','failed','cancelled','interrupted')",
                                   (older_than,))
            self._db.commit()
            return cur.rowcount

    # ------------------------------------------------------------------ cache
    def cache_get(self, key: str, ttl: float) -> Any | None:
        with self._lock:
            r = self._db.execute("SELECT value, created FROM cache WHERE key=?", (key,)).fetchone()
        if r and time.time() - r["created"] < ttl:
            return json.loads(r["value"])
        return None

    def cache_set(self, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO cache(key,value,created) VALUES(?,?,?)",
                             (key, json.dumps(value), time.time()))
            self._db.commit()

    def cache_clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM cache")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
