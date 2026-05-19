from __future__ import annotations

import os
import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

DB_PATH = Path(os.environ.get("PROTOCOL_REG_DB_PATH", "data/data.db"))
_sqlite_write_lock = threading.RLock()


class get_db_conn:
    """提供 auth_core 所需的最小 SQLite 连接上下文。"""

    def __init__(self, as_dict: bool = False, is_write: bool = False):
        self.as_dict = as_dict
        self.is_write = is_write
        self.conn: sqlite3.Connection | None = None

    def __enter__(self) -> sqlite3.Connection:
        if self.is_write:
            _sqlite_write_lock.acquire()
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        isolation_level = "IMMEDIATE" if self.is_write else None
        self.conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=isolation_level)
        if self.as_dict:
            self.conn.row_factory = sqlite3.Row
        return self.conn

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        if self.conn is None:
            return
        try:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
        finally:
            self.conn.close()
            if self.is_write:
                _sqlite_write_lock.release()


def get_cursor(conn: sqlite3.Connection, as_dict: bool = False) -> sqlite3.Cursor:
    return conn.cursor()


def execute_sql(cursor: sqlite3.Cursor, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
    return cursor.execute(sql, params)


def set_sys_kv(key: str, value: Any) -> None:
    val_str = json.dumps(value, ensure_ascii=False)
    with get_db_conn(is_write=True) as conn:
        cursor = get_cursor(conn)
        execute_sql(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS system_kv (
                `key` TEXT PRIMARY KEY,
                value TEXT
            )
            """,
        )
        execute_sql(
            cursor,
            "INSERT OR REPLACE INTO system_kv (`key`, value) VALUES (?, ?)",
            (key, val_str),
        )


def get_sys_kv(key: str, default: Any = None) -> Any:
    with get_db_conn() as conn:
        cursor = get_cursor(conn)
        execute_sql(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS system_kv (
                `key` TEXT PRIMARY KEY,
                value TEXT
            )
            """,
        )
        row = execute_sql(cursor, "SELECT value FROM system_kv WHERE `key` = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row[0])
    except Exception:
        return default
