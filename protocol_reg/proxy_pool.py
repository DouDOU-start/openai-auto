from __future__ import annotations

import json
from typing import Any

from .settings import resolve_proxy_pool


_STATE_KEY = "protocol_reg.proxy_round_robin.v1"


def pick_proxy(*candidates: object) -> str:
    return pick_proxy_from_pool(resolve_proxy_pool(*candidates))


def pick_proxy_from_pool(pool: tuple[str, ...]) -> str:
    if not pool:
        return ""

    signature = "\u001f".join(pool)

    from utils import db_manager

    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS system_kv (
                `key` TEXT PRIMARY KEY,
                value TEXT
            )
            """,
        )
        row = db_manager.execute_sql(
            cursor,
            "SELECT value FROM system_kv WHERE `key` = ?",
            (_STATE_KEY,),
        ).fetchone()
        state = _load_state(row[0]) if row else {}
        cursor_index = _safe_int(state.get("cursor"), 0)
        if state.get("signature") != signature or cursor_index < 0:
            cursor_index = 0
        proxy = pool[cursor_index % len(pool)]
        next_state = {
            "signature": signature,
            "cursor": cursor_index + 1,
        }
        db_manager.execute_sql(
            cursor,
            "INSERT OR REPLACE INTO system_kv (`key`, value) VALUES (?, ?)",
            (_STATE_KEY, json.dumps(next_state, ensure_ascii=False)),
        )
        return proxy


def _load_state(raw: Any) -> dict[str, Any]:
    try:
        data = json.loads(str(raw or "{}"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _safe_int(value: object, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default
