# -*- coding: utf-8 -*-
"""SQLite 持久化：任务运行状态、事件日志、KV（实例 id 等）。WAL 模式，线程安全。"""
import sqlite3
import threading
import time
from datetime import datetime

KEEP_EVENTS = 500


class Database:
    def __init__(self, path):
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS kv(
                key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS task_state(
                task_id TEXT PRIMARY KEY,
                last REAL DEFAULT 0,
                last_trigger_date TEXT DEFAULT '',
                executed INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT, level TEXT, task TEXT, channel TEXT, detail TEXT);
        """)
        self.conn.commit()

    # ---- KV ----
    def get_kv(self, key, default=None):
        with self._lock:
            row = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
            return row["value"] if row else default

    def set_kv(self, key, value):
        with self._lock:
            self.conn.execute(
                "INSERT INTO kv(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
            self.conn.commit()

    # ---- 任务状态 ----
    def get_task_state(self, task_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM task_state WHERE task_id=?", (task_id,)).fetchone()
            if row:
                return {"last": row["last"], "last_trigger_date": row["last_trigger_date"],
                        "executed": bool(row["executed"])}
            return {"last": 0.0, "last_trigger_date": "", "executed": False}

    def set_task_state(self, task_id, **fields):
        with self._lock:
            st = self.get_task_state(task_id)
            st.update(fields)
            self.conn.execute(
                "INSERT INTO task_state(task_id,last,last_trigger_date,executed) VALUES(?,?,?,?) "
                "ON CONFLICT(task_id) DO UPDATE SET last=excluded.last, "
                "last_trigger_date=excluded.last_trigger_date, executed=excluded.executed",
                (task_id, st["last"], st["last_trigger_date"], int(st["executed"])))
            self.conn.commit()

    # ---- 事件日志 ----
    def add_event(self, level, task, channel, detail):
        with self._lock:
            self.conn.execute(
                "INSERT INTO events(ts,level,task,channel,detail) VALUES(?,?,?,?,?)",
                (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), level, task, channel, detail))
            self.conn.execute(
                "DELETE FROM events WHERE id NOT IN "
                "(SELECT id FROM events ORDER BY id DESC LIMIT ?)", (KEEP_EVENTS,))
            self.conn.commit()

    def recent_events(self, limit=100):
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]

    def event_exists_today(self, task, keyword):
        """今日是否已写过该任务含关键字的提示事件（用于跳过类提示去重）。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM events WHERE task=? AND detail LIKE ? "
                "AND substr(ts,1,10)=? LIMIT 1",
                (task, "%" + keyword + "%",
                 datetime.now().strftime("%Y-%m-%d"))).fetchone()
            return row is not None
