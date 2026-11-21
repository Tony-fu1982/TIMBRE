"""SQLite WAL 持久层（SPEC §2）。单连接+锁；启动即建表与恢复。"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL CHECK(kind IN ('upload','download','dna','generate','convert')),
  status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','success','failed','cancelled')),
  stage TEXT NOT NULL DEFAULT '',
  progress TEXT NOT NULL DEFAULT '',
  input TEXT NOT NULL DEFAULT '',
  options TEXT NOT NULL DEFAULT '{}',
  result TEXT,
  suno_song_id TEXT NOT NULL DEFAULT '',
  render_id TEXT NOT NULL DEFAULT '',
  render_state TEXT NOT NULL DEFAULT '' CHECK(render_state IN ('','submitting','pending','succeeded','failed','unknown')),
  output_path TEXT NOT NULL DEFAULT '',
  error_code TEXT NOT NULL DEFAULT '',
  error_msg TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, id DESC);
CREATE TABLE IF NOT EXISTS job_logs(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id INTEGER NOT NULL,
  message TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_job_logs_job_seq ON job_logs(job_id, seq);
CREATE TABLE IF NOT EXISTS session_state(
  id INTEGER PRIMARY KEY CHECK(id=1),
  state TEXT NOT NULL DEFAULT 'not_logged_in',
  account TEXT NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO session_state(id) VALUES(1);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

TERMINAL = ("success", "failed", "cancelled")


def row_to_job(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["options"] = json.loads(d["options"] or "{}")
    d["result"] = json.loads(d["result"]) if d["result"] else None
    return d


class Database:
    def __init__(self, path: Path | str = config.DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.lock = threading.RLock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            self.conn.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES('download_dir',?)",
                (config.system_download_dir(),),
            )
            self.conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES('debug_logs','0')")
            self.conn.commit()

    def close(self):
        with self.lock:
            self.conn.close()

    # ---- 通用 ----
    def q(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args=()) -> sqlite3.Row | None:
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def ex(self, sql: str, args=()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # ---- jobs ----
    def insert_job(self, kind: str, input_: str, options: dict) -> dict:
        with self.lock:
            cur = self.conn.execute(
                "INSERT INTO jobs(kind,input,options) VALUES(?,?,?)",
                (kind, input_, json.dumps(options, ensure_ascii=False)),
            )
            self.conn.commit()
            return self.get_job(cur.lastrowid)

    def has_active(self) -> bool:
        return self.one("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1") is not None

    def get_job(self, job_id: int) -> dict | None:
        row = self.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        return row_to_job(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[dict]:
        return [row_to_job(r) for r in self.q("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))]

    def next_queued(self) -> dict | None:
        row = self.one("SELECT * FROM jobs WHERE status='queued' ORDER BY id ASC LIMIT 1")
        return row_to_job(row) if row else None

    def update_job(self, job_id: int, **fields) -> None:
        if not fields:
            return
        if "options" in fields:
            fields["options"] = json.dumps(fields["options"], ensure_ascii=False)
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], ensure_ascii=False) if fields["result"] is not None else None
        sets = ", ".join(f"{k}=?" for k in fields)
        self.ex(f"UPDATE jobs SET {sets}, updated_at=datetime('now') WHERE id=?", (*fields.values(), job_id))

    def log_seq(self, job_id: int) -> int:
        row = self.one("SELECT MAX(seq) AS m FROM job_logs WHERE job_id=?", (job_id,))
        return int(row["m"] or 0)

    def snapshot(self, job_id: int) -> dict:
        j = self.get_job(job_id)
        return {
            "id": job_id,
            "status": j["status"],
            "stage": j["stage"],
            "progress": j["progress"],
            "log_seq": self.log_seq(job_id),
        }

    def append_log(self, job_id: int, message: str) -> int:
        with self.lock:
            cur = self.conn.execute("INSERT INTO job_logs(job_id,message) VALUES(?,?)", (job_id, message))
            self.conn.execute(
                "UPDATE jobs SET progress=?, updated_at=datetime('now') WHERE id=?", (message, job_id)
            )
            self.conn.commit()
            return cur.lastrowid

    def logs_after(self, job_id: int, after_seq: int, limit: int = 200) -> dict:
        rows = self.q(
            "SELECT seq,message,created_at FROM job_logs WHERE job_id=? AND seq>? ORDER BY seq ASC LIMIT ?",
            (job_id, after_seq, limit + 1),
        )
        has_more = len(rows) > limit
        lines = [dict(r) for r in rows[:limit]]
        return {"lines": lines, "has_more": has_more}

    # ---- 启动恢复（SPEC §1）----
    def recover(self) -> None:
        self.ex("UPDATE jobs SET status='cancelled', error_msg='应用退出后，等待任务将取消', updated_at=datetime('now') WHERE status='queued'")
        self.ex("UPDATE jobs SET status='failed', error_code='INTERNAL_ERROR', error_msg='应用上次退出时任务中断', updated_at=datetime('now') WHERE status='running'")
        self.ex("UPDATE jobs SET render_state='unknown', updated_at=datetime('now') WHERE render_state='submitting'")

    # ---- session / settings ----
    def session(self) -> dict:
        return dict(self.one("SELECT state,account FROM session_state WHERE id=1"))

    def set_session(self, state: str, account: str = "") -> None:
        self.ex("UPDATE session_state SET state=?, account=? WHERE id=1", (state, account))

    def settings(self) -> dict:
        return {r["key"]: r["value"] for r in self.q("SELECT key,value FROM settings")}

    def set_setting(self, key: str, value: str) -> None:
        self.ex("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))