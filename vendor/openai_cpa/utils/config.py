from __future__ import annotations

from datetime import datetime, timedelta, timezone

DB_TYPE = "sqlite"
MYSQL_CFG: dict[str, object] = {}


def ts() -> str:
    """返回与原依赖兼容的东八区日志时间。"""
    return datetime.now(timezone(timedelta(hours=8))).strftime("%H:%M:%S")
