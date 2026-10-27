"""SQLite 存储:关注列表、任务运行记录、小时报告、社区日报与帖子来源记录。

不保存原始K线数组、API key 或 Cookie。每个线程使用独立连接。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS watchlist (
  inst_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1, added_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  job_id TEXT PRIMARY KEY,
  job_type TEXT NOT NULL,               -- hourly / daily / manual_hourly / manual_daily
  scope TEXT NOT NULL,                  -- inst_id 或 '*'
  slot_key TEXT NOT NULL,               -- 小时任务:UTC 整点 ISO;每日任务:调度时区逻辑日期
  scheduled_at INTEGER,
  started_at INTEGER, finished_at INTEGER,
  status TEXT NOT NULL,                 -- pending / running / succeeded / failed / skipped
  attempts INTEGER NOT NULL DEFAULT 0,
  error_code TEXT, error TEXT,
  model TEXT, prompt_version TEXT,
  llm_calls INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
  uncertain INTEGER NOT NULL DEFAULT 0, -- 远程调用结果不确定(可能已计费)
  report_id TEXT, created_at INTEGER NOT NULL,
  UNIQUE (job_type, scope, slot_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, job_type);
CREATE TABLE IF NOT EXISTS reports (
  report_id TEXT PRIMARY KEY, job_id TEXT, inst_id TEXT NOT NULL,
  analysis_as_of INTEGER NOT NULL, created_at INTEGER NOT NULL,
  strategy_version TEXT, model TEXT, prompt_version TEXT, model_status TEXT,
  community_report_id TEXT,
  final_verdict TEXT, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_inst ON reports(inst_id, analysis_as_of);
CREATE TABLE IF NOT EXISTS community_reports (
  report_id TEXT PRIMARY KEY, job_id TEXT, logical_date TEXT,
  window_start INTEGER, window_end INTEGER, generated_at INTEGER, expires_at INTEGER,
  collection_status TEXT, source_mode TEXT, model TEXT, body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_creports_gen ON community_reports(generated_at);
CREATE TABLE IF NOT EXISTS community_posts (
  post_id TEXT NOT NULL, source TEXT NOT NULL, url TEXT, author_id TEXT, author_name TEXT,
  publish_time INTEGER, collected_at INTEGER, text TEXT, symbols TEXT, likes INTEGER,
  comments INTEGER, views INTEGER, images INTEGER, status TEXT, last_report_id TEXT,
  PRIMARY KEY (post_id, source)
);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


class Store:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._local = threading.local()
        self._shared = None
        if path == ":memory:":   # 测试用:内存库只能共享单连接
            self._shared = sqlite3.connect(":memory:", check_same_thread=False)
            self._shared.row_factory = sqlite3.Row
        self._wlock = threading.Lock()
        self.conn().executescript(SCHEMA)

    def conn(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        c = getattr(self._local, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            self._local.c = c
        return c

    def _exec(self, sql: str, args=()):
        with self._wlock:
            c = self.conn()
            cur = c.execute(sql, args)
            c.commit()
            return cur

    def _all(self, sql: str, args=()) -> List[Dict]:
        return [dict(r) for r in self.conn().execute(sql, args).fetchall()]

    # ---------- 关注列表 ----------
    def ensure_watchlist(self, defaults: List[str]):
        if not self._all("SELECT 1 FROM watchlist LIMIT 1"):
            for inst in defaults:
                self.set_watch(inst, True)

    def set_watch(self, inst_id: str, on: bool):
        self._exec("INSERT INTO watchlist(inst_id, enabled, added_at) VALUES (?,?,?) "
                   "ON CONFLICT(inst_id) DO UPDATE SET enabled=excluded.enabled",
                   (inst_id, 1 if on else 0, now_ms()))

    def watchlist(self, enabled_only: bool = True) -> List[str]:
        q = "SELECT inst_id FROM watchlist" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY inst_id"
        return [r["inst_id"] for r in self._all(q)]

    def watchlist_rows(self) -> List[Dict]:
        return self._all("SELECT * FROM watchlist ORDER BY inst_id")

    # ---------- 任务 ----------
    def claim_job(self, job_type: str, scope: str, slot_key: str, scheduled_at: Optional[int],
                  status: str = "running") -> Optional[str]:
        """按唯一键插入任务;已存在则返回 None(防重复执行/重启后重复)。"""
        job_id = uuid.uuid4().hex[:16]
        t = now_ms()
        try:
            self._exec("INSERT INTO jobs(job_id, job_type, scope, slot_key, scheduled_at, started_at, "
                       "status, attempts, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                       (job_id, job_type, scope, slot_key, scheduled_at,
                        t if status == "running" else None, status,
                        1 if status == "running" else 0, t))
        except sqlite3.IntegrityError:
            return None
        return job_id

    def start_pending(self, job_id: str) -> bool:
        cur = self._exec("UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 "
                         "WHERE job_id=? AND status='pending'", (now_ms(), job_id))
        return cur.rowcount == 1

    def finish_job(self, job_id: str, status: str, **fields):
        cols = {"status": status, "finished_at": now_ms(), **fields}
        sets = ", ".join(f"{k}=?" for k in cols)
        self._exec(f"UPDATE jobs SET {sets} WHERE job_id=?", (*cols.values(), job_id))

    def job(self, job_id: str) -> Optional[Dict]:
        rows = self._all("SELECT * FROM jobs WHERE job_id=?", (job_id,))
        return rows[0] if rows else None

    def find_job(self, job_type: str, scope: str, slot_key: str) -> Optional[Dict]:
        rows = self._all("SELECT * FROM jobs WHERE job_type=? AND scope=? AND slot_key=?",
                         (job_type, scope, slot_key))
        return rows[0] if rows else None

    def jobs(self, limit: int = 50, job_type: Optional[str] = None, status: Optional[str] = None) -> List[Dict]:
        q, args = "SELECT * FROM jobs WHERE 1=1", []
        if job_type:
            q += " AND job_type=?"
            args.append(job_type)
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return self._all(q, args)

    def pending_manual(self) -> List[Dict]:
        return self._all("SELECT * FROM jobs WHERE status='pending' AND job_type LIKE 'manual_%' "
                         "ORDER BY created_at")

    def recover_running(self):
        """进程异常退出遗留的 running 任务:标记失败且结果不确定,不自动重跑(避免重复计费)。"""
        self._exec("UPDATE jobs SET status='failed', error_code='interrupted', uncertain=1, "
                   "error='进程在任务执行中退出', finished_at=? WHERE status='running'", (now_ms(),))

    def latest_slot(self, job_type: str, scope: str = "*") -> Optional[str]:
        rows = self._all("SELECT MAX(slot_key) AS s FROM jobs WHERE job_type=? AND scope=?",
                         (job_type, scope))
        return rows[0]["s"] if rows else None

    # ---------- 小时报告 ----------
    def save_report(self, rep: Dict):
        self._exec("INSERT INTO reports(report_id, job_id, inst_id, analysis_as_of, created_at, "
                   "strategy_version, model, prompt_version, model_status, community_report_id, "
                   "final_verdict, body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (rep["report_id"], rep.get("job_id"), rep["inst_id"], rep["analysis_as_of"],
                    rep["created_at"], rep.get("strategy_version"), rep.get("model"),
                    rep.get("prompt_version"), rep.get("model_status"),
                    rep.get("community_report_id"), rep["final"]["final"],
                    json.dumps(rep, ensure_ascii=False)))

    def reports(self, inst_id: Optional[str] = None, limit: int = 24) -> List[Dict]:
        q, args = "SELECT body FROM reports", []
        if inst_id:
            q += " WHERE inst_id=?"
            args.append(inst_id)
        q += " ORDER BY analysis_as_of DESC LIMIT ?"
        args.append(limit)
        return [json.loads(r["body"]) for r in self._all(q, args)]

    def report(self, report_id: str) -> Optional[Dict]:
        rows = self._all("SELECT body FROM reports WHERE report_id=?", (report_id,))
        return json.loads(rows[0]["body"]) if rows else None

    # ---------- 社区 ----------
    def save_community_report(self, rep: Dict):
        self._exec("INSERT INTO community_reports(report_id, job_id, logical_date, window_start, "
                   "window_end, generated_at, expires_at, collection_status, source_mode, model, body) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (rep["report_id"], rep.get("job_id"), rep.get("logical_date"), rep["window_start"],
                    rep["window_end"], rep["generated_at"], rep["expires_at"],
                    rep["collection_status"], rep["source_mode"], rep.get("model"),
                    json.dumps(rep, ensure_ascii=False)))

    def valid_community_report(self, as_of: int) -> Optional[Dict]:
        """分析时刻有效的最新日报:生成不晚于 as_of 且未过期。"""
        rows = self._all("SELECT body FROM community_reports WHERE generated_at<=? AND expires_at>? "
                         "ORDER BY generated_at DESC LIMIT 1", (as_of, as_of))
        return json.loads(rows[0]["body"]) if rows else None

    def community_reports(self, limit: int = 10) -> List[Dict]:
        return [json.loads(r["body"]) for r in
                self._all("SELECT body FROM community_reports ORDER BY generated_at DESC LIMIT ?", (limit,))]

    def community_report(self, report_id: str) -> Optional[Dict]:
        rows = self._all("SELECT body FROM community_reports WHERE report_id=?", (report_id,))
        return json.loads(rows[0]["body"]) if rows else None

    def save_posts(self, posts: List[Dict], report_id: str):
        with self._wlock:
            c = self.conn()
            c.executemany(
                "INSERT INTO community_posts(post_id, source, url, author_id, author_name, publish_time, "
                "collected_at, text, symbols, likes, comments, views, images, status, last_report_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(post_id, source) DO UPDATE SET "
                "url=excluded.url, author_id=COALESCE(excluded.author_id, author_id), "
                "author_name=COALESCE(excluded.author_name, author_name), collected_at=excluded.collected_at, "
                "text=excluded.text, symbols=excluded.symbols, likes=excluded.likes, comments=excluded.comments, "
                "views=excluded.views, images=excluded.images, status=excluded.status, "
                "last_report_id=excluded.last_report_id",
                [(p["post_id"], p["source"], p.get("url"), p.get("author_id"), p.get("author_name"),
                  p.get("publish_time"), p.get("collected_at"), p.get("text"),
                  json.dumps(p.get("symbols") or []), p.get("likes"), p.get("comments"), p.get("views"),
                  p.get("images"), p.get("status"), report_id) for p in posts])
            c.commit()
