"""
SQLite state management:
  - id_map: old_id → new_id mapping (idempotency)
  - jobs: run history and status
  - log_entries: per-job detailed log
"""

from __future__ import annotations

import sqlite3
import time
import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, asdict
from enum import Enum
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS id_map (
    old_id      INTEGER PRIMARY KEY,
    new_id      INTEGER NOT NULL,
    post_type   TEXT,
    synced_at   REAL
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type    TEXT NOT NULL,   -- 'motor_a' | 'motor_b' | 'polylang_verify'
    mode        TEXT NOT NULL,   -- 'dry_run' | 'sync' | 'delete'
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|error
    started_at  REAL,
    finished_at REAL,
    summary     TEXT DEFAULT '{}',   -- JSON summary counts
    error_msg   TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS log_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id      INTEGER NOT NULL,
    ts          REAL NOT NULL,
    level       TEXT NOT NULL DEFAULT 'INFO',
    message     TEXT NOT NULL,
    FOREIGN KEY(job_id) REFERENCES jobs(id)
);

CREATE INDEX IF NOT EXISTS idx_log_job ON log_entries(job_id);
"""


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    id: int
    job_type: str
    mode: str
    status: str
    started_at: Optional[float]
    finished_at: Optional[float]
    summary: Dict
    error_msg: str


class StateDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # -- ID mapping ----------------------------------------------------------

    def get_new_id(self, old_id: int) -> Optional[int]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT new_id FROM id_map WHERE old_id=?", (old_id,)
            ).fetchone()
            return row["new_id"] if row else None

    def upsert_id_map(self, old_id: int, new_id: int, post_type: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO id_map(old_id, new_id, post_type, synced_at) "
                "VALUES (?, ?, ?, ?)",
                (old_id, new_id, post_type, time.time()),
            )

    def get_all_id_maps(self) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT old_id, new_id, post_type, synced_at FROM id_map ORDER BY old_id"
            ).fetchall()
            return [dict(r) for r in rows]

    # -- Jobs ----------------------------------------------------------------

    def create_job(self, job_type: str, mode: str) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO jobs(job_type, mode, status, started_at, summary) "
                "VALUES (?, ?, 'running', ?, '{}')",
                (job_type, mode, time.time()),
            )
            return cur.lastrowid

    def finish_job(self, job_id: int, summary: Dict, error: str = "") -> None:
        status = JobStatus.ERROR if error else JobStatus.DONE
        with self._conn() as conn:
            conn.execute(
                "UPDATE jobs SET status=?, finished_at=?, summary=?, error_msg=? WHERE id=?",
                (status, time.time(), json.dumps(summary), error, job_id),
            )

    def get_job(self, job_id: int) -> Optional[Job]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                return None
            return Job(
                id=row["id"],
                job_type=row["job_type"],
                mode=row["mode"],
                status=row["status"],
                started_at=row["started_at"],
                finished_at=row["finished_at"],
                summary=json.loads(row["summary"] or "{}"),
                error_msg=row["error_msg"] or "",
            )

    def list_jobs(self, limit: int = 50) -> List[Job]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                Job(
                    id=r["id"],
                    job_type=r["job_type"],
                    mode=r["mode"],
                    status=r["status"],
                    started_at=r["started_at"],
                    finished_at=r["finished_at"],
                    summary=json.loads(r["summary"] or "{}"),
                    error_msg=r["error_msg"] or "",
                )
                for r in rows
            ]

    # -- Logs ----------------------------------------------------------------

    def log(self, job_id: int, message: str, level: str = "INFO") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO log_entries(job_id, ts, level, message) VALUES(?,?,?,?)",
                (job_id, time.time(), level, message),
            )

    def get_job_logs(self, job_id: int, limit: int = 500) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT ts, level, message FROM log_entries "
                "WHERE job_id=? ORDER BY id DESC LIMIT ?",
                (job_id, limit),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def get_recent_logs(self, limit: int = 200) -> List[Dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT l.ts, l.level, l.message, j.job_type, j.mode, l.job_id "
                "FROM log_entries l JOIN jobs j ON l.job_id=j.id "
                "ORDER BY l.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]


# Singleton per state_dir
_instances: Dict[str, StateDB] = {}


def get_state_db(state_dir: str) -> StateDB:
    if state_dir not in _instances:
        db_path = str(Path(state_dir) / "state.db")
        _instances[state_dir] = StateDB(db_path)
    return _instances[state_dir]
