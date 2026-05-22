from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

NULL_VALUE = "null"
ACCOUNT_FIELD_COUNT = 5
ACCOUNT_STATUS_ACTIVE = "active"
ACCOUNT_STATUS_ABANDONED = "废弃"
STOCK_STATUS_IN = "未出库"
STOCK_STATUS_OUT = "出库"
STOCK_STATUSES = {STOCK_STATUS_IN, STOCK_STATUS_OUT}
AUTH_ROLE_ADMIN = "admin"
AUTH_ROLE_OPERATOR = "operator"
AUTH_ROLES = {AUTH_ROLE_ADMIN, AUTH_ROLE_OPERATOR}
AUTH_STATUS_ACTIVE = "active"
AUTH_STATUS_DISABLED = "disabled"
AUTH_STATUSES = {AUTH_STATUS_ACTIVE, AUTH_STATUS_DISABLED}
DEFAULT_OPERATOR_PERMISSIONS = (
    "view_subscription_accounts",
    "claim_subscription_account",
    "mark_subscription_done",
    "mark_subscription_failed",
)
ADMIN_PERMISSIONS = ("*",)
WEB_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
PASSWORD_HASH_ITERATIONS = 390_000
SUBSCRIPTION_STATUS_PENDING = "待订阅"
SUBSCRIPTION_STATUS_CLAIMED = "处理中"
SUBSCRIPTION_STATUS_MARKED = "已点击订阅"
SUBSCRIPTION_STATUS_VERIFIED = "已确认订阅"
SUBSCRIPTION_STATUS_FAILED = "订阅失败"
SUBSCRIPTION_STATUSES = {
    SUBSCRIPTION_STATUS_PENDING,
    SUBSCRIPTION_STATUS_CLAIMED,
    SUBSCRIPTION_STATUS_MARKED,
    SUBSCRIPTION_STATUS_VERIFIED,
    SUBSCRIPTION_STATUS_FAILED,
}
_file_write_lock = threading.RLock()


def init_accounts_db() -> None:
    """初始化账号数据库表，不影响 auth_core 已使用的 system_kv 表。"""

    db_manager = _db_manager()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password TEXT NOT NULL,
                subscription_type TEXT DEFAULT 'null',
                refresh_token TEXT DEFAULT 'null',
                session_json TEXT DEFAULT 'null',
                checkout_url TEXT DEFAULT 'null',
                login_session_json TEXT DEFAULT 'null',
                auth_token_json TEXT DEFAULT 'null',
                checkout_json TEXT DEFAULT 'null',
                subscription_status TEXT DEFAULT '待订阅',
                subscription_operator_id INTEGER,
                subscription_claimed_at TEXT,
                subscription_claim_expires_at TEXT,
                subscription_marked_at TEXT,
                subscription_verified_at TEXT,
                subscription_note TEXT DEFAULT 'null',
                stock_status TEXT DEFAULT '未出库',
                status TEXT DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT,
                last_authorized_at TEXT
            )
            """,
        )
        _ensure_accounts_column(cursor, "checkout_url", "TEXT DEFAULT 'null'")
        _ensure_accounts_column(cursor, "login_session_json", "TEXT DEFAULT 'null'")
        _ensure_accounts_column(cursor, "auth_token_json", "TEXT DEFAULT 'null'")
        _ensure_accounts_column(cursor, "checkout_json", "TEXT DEFAULT 'null'")
        _ensure_accounts_column(cursor, "subscription_status", "TEXT DEFAULT '待订阅'")
        _ensure_accounts_column(cursor, "subscription_operator_id", "INTEGER")
        _ensure_accounts_column(cursor, "subscription_claimed_at", "TEXT")
        _ensure_accounts_column(cursor, "subscription_claim_expires_at", "TEXT")
        _ensure_accounts_column(cursor, "subscription_marked_at", "TEXT")
        _ensure_accounts_column(cursor, "subscription_verified_at", "TEXT")
        _ensure_accounts_column(cursor, "subscription_note", "TEXT DEFAULT 'null'")
        _ensure_accounts_column(cursor, "stock_status", "TEXT DEFAULT '未出库'")
        _drop_accounts_column(cursor, "source")
        db_manager.execute_sql(
            cursor,
            "UPDATE accounts SET stock_status = ? WHERE lower(stock_status) IN ('out', 'sold', 'used', '1', 'true', 'yes')",
            (STOCK_STATUS_OUT,),
        )
        db_manager.execute_sql(
            cursor,
            "UPDATE accounts SET stock_status = ? WHERE stock_status IS NULL OR stock_status = '' OR stock_status NOT IN (?, ?)",
            (STOCK_STATUS_IN, STOCK_STATUS_IN, STOCK_STATUS_OUT),
        )
        db_manager.execute_sql(
            cursor,
            "UPDATE accounts SET subscription_status = ? WHERE subscription_status IS NULL OR subscription_status = '' OR subscription_status NOT IN (?, ?, ?, ?, ?)",
            (
                SUBSCRIPTION_STATUS_PENDING,
                SUBSCRIPTION_STATUS_PENDING,
                SUBSCRIPTION_STATUS_CLAIMED,
                SUBSCRIPTION_STATUS_MARKED,
                SUBSCRIPTION_STATUS_VERIFIED,
                SUBSCRIPTION_STATUS_FAILED,
            ),
        )
        db_manager.execute_sql(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)",
        )
        db_manager.execute_sql(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_accounts_stock_status ON accounts(stock_status)",
        )
        db_manager.execute_sql(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_accounts_subscription_status ON accounts(subscription_status)",
        )
        db_manager.execute_sql(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_accounts_subscription_operator ON accounts(subscription_operator_id)",
        )
        db_manager.execute_sql(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_accounts_updated_at ON accounts(updated_at)",
        )
        db_manager.execute_sql(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_accounts_created_at ON accounts(created_at)",
        )
        _init_web_auth_tables(cursor)


def _init_web_auth_tables(cursor: Any) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS web_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE COLLATE NOCASE,
            display_name TEXT DEFAULT '',
            role TEXT NOT NULL DEFAULT 'operator',
            permissions_json TEXT DEFAULT '[]',
            password_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            last_login_at TEXT,
            last_login_ip TEXT,
            last_login_user_agent TEXT
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_web_users_role ON web_users(role)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_web_users_status ON web_users(status)")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS web_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_token_hash TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            revoked_at TEXT,
            ip TEXT,
            user_agent TEXT,
            FOREIGN KEY(user_id) REFERENCES web_users(id) ON DELETE CASCADE
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_web_sessions_user_id ON web_sessions(user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_web_sessions_expires_at ON web_sessions(expires_at)")
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_user_id INTEGER,
            action TEXT NOT NULL,
            target_type TEXT DEFAULT '',
            target_id TEXT DEFAULT '',
            detail_json TEXT DEFAULT 'null',
            ip TEXT DEFAULT '',
            user_agent TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY(actor_user_id) REFERENCES web_users(id) ON DELETE SET NULL
        )
        """
    )
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_user_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at ON audit_logs(created_at)")


def save_account_storage(
    email: str,
    password: str,
    token_data: dict[str, Any] | None = None,
    *,
    source: str = "manual",
) -> None:
    """把账号主数据写入数据库。"""

    init_accounts_db()
    fields = _compact_account_fields(email, password, token_data or {})
    _upsert_account_db(fields, source)


def load_account_records() -> list[dict[str, str]]:
    """读取数据库账号。"""

    return load_accounts_db()


def load_accounts_db() -> list[dict[str, str]]:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        rows = db_manager.execute_sql(
            cursor,
            """
            SELECT email, password, subscription_type, refresh_token, session_json, checkout_url,
                   login_session_json, auth_token_json, checkout_json,
                   subscription_status, subscription_operator_id, subscription_claimed_at,
                   subscription_claim_expires_at, subscription_marked_at, subscription_verified_at,
                   subscription_note, stock_status,
                   status, created_at, updated_at, last_login_at, last_authorized_at
            FROM accounts
            ORDER BY id ASC
            """,
        ).fetchall()
    return [_account_from_row(row) for row in rows]


def _build_account_filter(search: str, status: str, plan: str, stock_status: str) -> tuple[str, tuple[str, ...]]:
    where: list[str] = []
    params: list[str] = []
    search_value = search.strip()
    if search_value:
        like = f"%{search_value}%"
        where.append(
            "(email LIKE ? OR subscription_type LIKE ? OR status LIKE ? OR stock_status LIKE ? OR checkout_url LIKE ?)"
        )
        params.extend([like, like, like, like, like])
    if status and status != "all":
        where.append("status = ?")
        params.append(status)
    if plan and plan != "all":
        where.append("subscription_type = ?")
        params.append(plan)
    if stock_status and stock_status != "all":
        where.append("stock_status = ?")
        params.append(_normalize_stock_status(stock_status))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return where_sql, tuple(params)


def list_account_rows(
    search: str = "",
    status: str = "",
    plan: str = "",
    stock_status: str = "",
    page: int = 1,
    page_size: int = 0,
) -> list[dict[str, str]]:
    """按条件读取账号管理页列表。page_size=0 表示不分页，返回所有匹配项。"""

    init_accounts_db()
    where_sql, params = _build_account_filter(search, status, plan, stock_status)

    limit_sql = ""
    extra_params: tuple[Any, ...] = ()
    if page_size and page_size > 0:
        effective_page = max(1, int(page))
        offset = (effective_page - 1) * int(page_size)
        limit_sql = "LIMIT ? OFFSET ?"
        extra_params = (int(page_size), int(offset))

    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        rows = db_manager.execute_sql(
            cursor,
            f"""
            SELECT id, email, password, subscription_type, refresh_token, session_json, checkout_url,
                   login_session_json, auth_token_json, checkout_json,
                   subscription_status, subscription_operator_id, subscription_claimed_at,
                   subscription_claim_expires_at, subscription_marked_at, subscription_verified_at,
                   subscription_note, stock_status,
                   status, created_at, updated_at, last_login_at, last_authorized_at
            FROM accounts
            {where_sql}
            ORDER BY created_at DESC, id DESC
            {limit_sql}
            """,
            params + extra_params,
        ).fetchall()
    return [_account_from_row(row) for row in rows]


def list_account_rows_by_ids(ids: list[int]) -> list[dict[str, str]]:
    """按给定账号 ID 顺序读取账号；不存在的 ID 会被跳过。"""

    clean_ids: list[int] = []
    seen: set[int] = set()
    for raw_id in ids:
        try:
            account_id = int(raw_id)
        except Exception:
            continue
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        clean_ids.append(account_id)
    if not clean_ids:
        return []

    init_accounts_db()
    rows_by_id: dict[int, dict[str, str]] = {}
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        for start in range(0, len(clean_ids), 900):
            chunk = clean_ids[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = db_manager.execute_sql(
                cursor,
                f"""
                SELECT id, email, password, subscription_type, refresh_token, session_json, checkout_url,
                       login_session_json, auth_token_json, checkout_json,
                       subscription_status, subscription_operator_id, subscription_claimed_at,
                       subscription_claim_expires_at, subscription_marked_at, subscription_verified_at,
                       subscription_note, stock_status,
                       status, created_at, updated_at, last_login_at, last_authorized_at
                FROM accounts
                WHERE id IN ({placeholders})
                """,
                tuple(chunk),
            ).fetchall()
            for row in rows:
                account = _account_from_row(row)
                try:
                    rows_by_id[int(account.get("id") or 0)] = account
                except Exception:
                    continue
    return [rows_by_id[account_id] for account_id in clean_ids if account_id in rows_by_id]


def count_account_rows(search: str = "", status: str = "", plan: str = "", stock_status: str = "") -> int:
    """返回按条件过滤后的账号总数。"""

    init_accounts_db()
    where_sql, params = _build_account_filter(search, status, plan, stock_status)
    db_manager = _db_manager()
    with db_manager.get_db_conn() as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(
            cursor,
            f"SELECT COUNT(*) FROM accounts {where_sql}",
            params,
        ).fetchone()
    return int(row[0] if row is not None else 0)


def get_account_db(account_id: int) -> dict[str, str] | None:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(
            cursor,
            """
            SELECT id, email, password, subscription_type, refresh_token, session_json, checkout_url,
                   login_session_json, auth_token_json, checkout_json,
                   subscription_status, subscription_operator_id, subscription_claimed_at,
                   subscription_claim_expires_at, subscription_marked_at, subscription_verified_at,
                   subscription_note, stock_status,
                   status, created_at, updated_at, last_login_at, last_authorized_at
            FROM accounts
            WHERE id = ?
            """,
            (account_id,),
        ).fetchone()
    return _account_from_row(row) if row is not None else None


def _get_account_db_by_email(email: str) -> dict[str, str] | None:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(
            cursor,
            """
            SELECT id, email, password, subscription_type, refresh_token, session_json, checkout_url,
                   login_session_json, auth_token_json, checkout_json,
                   subscription_status, subscription_operator_id, subscription_claimed_at,
                   subscription_claim_expires_at, subscription_marked_at, subscription_verified_at,
                   subscription_note, stock_status,
                   status, created_at, updated_at, last_login_at, last_authorized_at
            FROM accounts
            WHERE email = ?
            """,
            (_field(email).lower(),),
        ).fetchone()
    return _account_from_row(row) if row is not None else None


def save_account_db_record(data: dict[str, Any], account_id: int | None = None) -> dict[str, str]:
    """保存管理页账号表单，空的可选字段会明确写为 null。"""

    fields = _normalize_account_fields(
        [
            str(data.get("email") or ""),
            str(data.get("password") or ""),
            str(data.get("subscription_type") or ""),
            str(data.get("refresh_token") or ""),
            str(data.get("session") or data.get("session_json") or ""),
        ]
    )
    if fields is None:
        raise ValueError("邮箱和密码不能为空")

    status = _field(data.get("status") or ACCOUNT_STATUS_ACTIVE)
    if status == NULL_VALUE:
        status = ACCOUNT_STATUS_ACTIVE
    stock_status = _normalize_stock_status(data.get("stock_status") or STOCK_STATUS_IN)
    checkout_url = _field(data.get("checkout_url") or NULL_VALUE)

    init_accounts_db()
    db_manager = _db_manager()
    now = utc_now()
    with db_manager.get_db_conn(as_dict=True, is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        if account_id is not None:
            existing = db_manager.execute_sql(
                cursor,
                "SELECT id, checkout_url, created_at, last_login_at, last_authorized_at FROM accounts WHERE id = ?",
                (account_id,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"账号不存在: {account_id}")
            saved_checkout_url = checkout_url if checkout_url != NULL_VALUE else _field(existing["checkout_url"])
            db_manager.execute_sql(
                cursor,
                """
                UPDATE accounts
                SET email = ?,
                    password = ?,
                    subscription_type = ?,
                    refresh_token = ?,
                    session_json = ?,
                    checkout_url = ?,
                    stock_status = ?,
                    status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (*fields, saved_checkout_url, stock_status, status, now, account_id),
            )
            saved_id = account_id
        else:
            existing = db_manager.execute_sql(
                cursor,
                "SELECT id, checkout_url FROM accounts WHERE email = ?",
                (fields[0],),
            ).fetchone()
            if existing is None:
                db_manager.execute_sql(
                    cursor,
                    """
                    INSERT INTO accounts (
                        email, password, subscription_type, refresh_token, session_json, checkout_url, stock_status,
                        status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (*fields, checkout_url, stock_status, status, now, now),
                )
                saved_id = int(cursor.lastrowid)
            else:
                saved_id = int(existing["id"])
                saved_checkout_url = checkout_url if checkout_url != NULL_VALUE else _field(existing["checkout_url"])
                db_manager.execute_sql(
                    cursor,
                    """
                    UPDATE accounts
                    SET password = ?,
                        subscription_type = ?,
                        refresh_token = ?,
                        session_json = ?,
                        checkout_url = ?,
                        stock_status = ?,
                        status = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        fields[1],
                        fields[2],
                        fields[3],
                        fields[4],
                        saved_checkout_url,
                        stock_status,
                        status,
                        now,
                        saved_id,
                    ),
                )

    saved = get_account_db(saved_id)
    if saved is None:
        raise RuntimeError("账号保存后读取失败")
    return saved


def update_account_checkout_url_db(
    email: str,
    checkout_url: str,
    checkout_data: dict[str, Any] | None = None,
) -> dict[str, str] | None:
    """按邮箱更新账号 checkout 长链接和可选的完整 checkout 响应。"""

    normalized_email = _field(email).lower()
    normalized_url = _field(checkout_url)
    if normalized_email == NULL_VALUE or normalized_url == NULL_VALUE:
        return None
    normalized_checkout = _field(json.dumps(checkout_data, ensure_ascii=False, separators=(",", ":"))) if checkout_data else NULL_VALUE

    init_accounts_db()
    db_manager = _db_manager()
    now = utc_now()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        if normalized_checkout == NULL_VALUE:
            db_manager.execute_sql(
                cursor,
                """
                UPDATE accounts
                SET checkout_url = ?,
                    subscription_status = ?,
                    subscription_operator_id = NULL,
                    subscription_claimed_at = NULL,
                    subscription_claim_expires_at = NULL,
                    subscription_marked_at = NULL,
                    subscription_verified_at = NULL,
                    subscription_note = ?,
                    updated_at = ?
                WHERE email = ?
                """,
                (normalized_url, SUBSCRIPTION_STATUS_PENDING, NULL_VALUE, now, normalized_email),
            )
        else:
            db_manager.execute_sql(
                cursor,
                """
                UPDATE accounts
                SET checkout_url = ?,
                    checkout_json = ?,
                    subscription_status = ?,
                    subscription_operator_id = NULL,
                    subscription_claimed_at = NULL,
                    subscription_claim_expires_at = NULL,
                    subscription_marked_at = NULL,
                    subscription_verified_at = NULL,
                    subscription_note = ?,
                    updated_at = ?
                WHERE email = ?
                """,
                (
                    normalized_url,
                    normalized_checkout,
                    SUBSCRIPTION_STATUS_PENDING,
                    NULL_VALUE,
                    now,
                    normalized_email,
                ),
            )
        if cursor.rowcount <= 0:
            return None

    account = _get_account_db_by_email(normalized_email)
    if account is None:
        raise RuntimeError("账号 checkout 长链接更新后读取失败")
    return account


def update_account_subscription_type_db(account_id: int, subscription_type: str) -> dict[str, str]:
    """更新账号订阅类型。"""

    normalized = _field(subscription_type)
    if normalized == NULL_VALUE:
        raise ValueError("订阅类型不能为空")

    init_accounts_db()
    db_manager = _db_manager()
    now = utc_now()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            "UPDATE accounts SET subscription_type = ?, updated_at = ? WHERE id = ?",
            (normalized, now, account_id),
        )
        if cursor.rowcount <= 0:
            raise KeyError(f"账号不存在: {account_id}")
    account = get_account_db(account_id)
    if account is None:
        raise RuntimeError("账号订阅类型更新后读取失败")
    return account


def update_account_stock_status_db(account_id: int, stock_status: str) -> dict[str, str]:
    """更新账号出库状态。"""

    normalized = _normalize_stock_status(stock_status)
    init_accounts_db()
    db_manager = _db_manager()
    now = utc_now()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(
            cursor,
            "SELECT status FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"账号不存在: {account_id}")
        current_status = _field(row[0])
        if normalized == STOCK_STATUS_OUT and current_status == ACCOUNT_STATUS_ABANDONED:
            raise ValueError("废弃账号不允许出库")
        db_manager.execute_sql(
            cursor,
            "UPDATE accounts SET stock_status = ?, updated_at = ? WHERE id = ?",
            (normalized, now, account_id),
        )
        if cursor.rowcount <= 0:
            raise KeyError(f"账号不存在: {account_id}")
    account = get_account_db(account_id)
    if account is None:
        raise RuntimeError("账号出库状态更新后读取失败")
    return account


def update_account_status_db(account_id: int, status: str) -> dict[str, str]:
    """更新账号业务状态。"""

    normalized = _field(status)
    if normalized == NULL_VALUE:
        normalized = ACCOUNT_STATUS_ACTIVE

    init_accounts_db()
    db_manager = _db_manager()
    now = utc_now()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            "UPDATE accounts SET status = ?, updated_at = ? WHERE id = ?",
            (normalized, now, account_id),
        )
        if cursor.rowcount <= 0:
            raise KeyError(f"账号不存在: {account_id}")
    account = get_account_db(account_id)
    if account is None:
        raise RuntimeError("账号状态更新后读取失败")
    return account


def delete_account_db(account_id: int) -> bool:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(cursor, "DELETE FROM accounts WHERE id = ?", (account_id,))
        return cursor.rowcount > 0


def account_stats_db() -> dict[str, Any]:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn() as conn:
        cursor = db_manager.get_cursor(conn)
        total = cursor.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]
        with_rt = cursor.execute(
            "SELECT COUNT(*) FROM accounts WHERE refresh_token IS NOT NULL AND refresh_token != 'null' AND refresh_token != ''"
        ).fetchone()[0]
        with_session = cursor.execute(
            "SELECT COUNT(*) FROM accounts WHERE session_json IS NOT NULL AND session_json != 'null' AND session_json != ''"
        ).fetchone()[0]
        with_checkout = cursor.execute(
            "SELECT COUNT(*) FROM accounts WHERE checkout_url IS NOT NULL AND checkout_url != 'null' AND checkout_url != ''"
        ).fetchone()[0]
        with_login_session = cursor.execute(
            "SELECT COUNT(*) FROM accounts WHERE login_session_json IS NOT NULL AND login_session_json != 'null' AND login_session_json != ''"
        ).fetchone()[0]
        with_auth_token = cursor.execute(
            "SELECT COUNT(*) FROM accounts WHERE auth_token_json IS NOT NULL AND auth_token_json != 'null' AND auth_token_json != ''"
        ).fetchone()[0]
        plans = cursor.execute(
            "SELECT subscription_type, COUNT(*) FROM accounts GROUP BY subscription_type ORDER BY COUNT(*) DESC"
        ).fetchall()
        statuses = cursor.execute(
            "SELECT status, COUNT(*) FROM accounts GROUP BY status ORDER BY COUNT(*) DESC"
        ).fetchall()
        stock_statuses = cursor.execute(
            "SELECT stock_status, COUNT(*) FROM accounts GROUP BY stock_status ORDER BY COUNT(*) DESC"
        ).fetchall()
        subscription_statuses = cursor.execute(
            "SELECT subscription_status, COUNT(*) FROM accounts GROUP BY subscription_status ORDER BY COUNT(*) DESC"
        ).fetchall()
    return {
        "total": total,
        "with_rt": with_rt,
        "with_session": with_session,
        "with_checkout": with_checkout,
        "with_login_session": with_login_session,
        "with_auth_token": with_auth_token,
        "plans": {str(plan or NULL_VALUE): count for plan, count in plans},
        "statuses": {str(item_status or NULL_VALUE): count for item_status, count in statuses},
        "stock_statuses": _stock_status_counts(stock_statuses),
        "subscription_statuses": {
            _normalize_subscription_status(item_status): int(count)
            for item_status, count in subscription_statuses
        },
    }


def count_subscription_queue_db() -> int:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn() as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(
            cursor,
            """
            SELECT COUNT(*)
            FROM accounts
            WHERE stock_status = ?
              AND checkout_url IS NOT NULL AND checkout_url != 'null' AND checkout_url != ''
            """,
            (STOCK_STATUS_IN,),
        ).fetchone()
    return int(row[0] if row is not None else 0)


def list_subscription_queue_db(
    *,
    page: int = 1,
    page_size: int = 50,
    operator_user_id: int | None = None,
    include_processing: bool = True,
) -> list[dict[str, str]]:
    init_accounts_db()
    effective_page = max(1, int(page))
    effective_page_size = max(1, min(500, int(page_size)))
    offset = (effective_page - 1) * effective_page_size
    params: list[Any] = [STOCK_STATUS_IN, SUBSCRIPTION_STATUS_PENDING, SUBSCRIPTION_STATUS_FAILED]
    operator_clause = ""
    if operator_user_id is not None:
        operator_clause = " OR subscription_operator_id = ?"
        params.append(int(operator_user_id))
    processing_clause = ""
    if include_processing:
        if operator_user_id is None:
            processing_clause = " OR (subscription_status = ? AND subscription_operator_id IS NOT NULL)"
            params.append(SUBSCRIPTION_STATUS_CLAIMED)
        else:
            processing_clause = " OR (subscription_status = ? AND subscription_operator_id = ?)"
            params.extend([SUBSCRIPTION_STATUS_CLAIMED, int(operator_user_id)])
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        rows = db_manager.execute_sql(
            cursor,
            f"""
            SELECT id, email, password, subscription_type, refresh_token, session_json, checkout_url,
                   login_session_json, auth_token_json, checkout_json,
                   subscription_status, subscription_operator_id, subscription_claimed_at,
                   subscription_claim_expires_at, subscription_marked_at, subscription_verified_at,
                   subscription_note, stock_status,
                   status, created_at, updated_at, last_login_at, last_authorized_at
            FROM accounts
            WHERE stock_status = ?
              AND checkout_url IS NOT NULL AND checkout_url != 'null' AND checkout_url != ''
              AND (
                    subscription_status IN (?, ?)
                    {operator_clause}
                    {processing_clause}
                  )
            ORDER BY
                CASE subscription_status
                    WHEN ? THEN 0
                    WHEN ? THEN 1
                    WHEN ? THEN 2
                    ELSE 3
                END,
                created_at DESC,
                id DESC
            LIMIT ? OFFSET ?
            """,
            (
                *params,
                SUBSCRIPTION_STATUS_PENDING,
                SUBSCRIPTION_STATUS_CLAIMED,
                SUBSCRIPTION_STATUS_FAILED,
                effective_page_size,
                offset,
            ),
        ).fetchall()
    return [_account_from_row(row) for row in rows]


def claim_subscription_account_db(account_id: int, operator_user_id: int, *, claim_minutes: int = 30) -> dict[str, str]:
    init_accounts_db()
    now = utc_now()
    operator_id = _field(operator_user_id)
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=max(5, int(claim_minutes)))).isoformat()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True, is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(
            cursor,
            """
            SELECT subscription_status, subscription_operator_id, subscription_claim_expires_at
            FROM accounts
            WHERE id = ?
            """,
            (int(account_id),),
        ).fetchone()
        if row is None:
            raise KeyError(f"账号不存在: {account_id}")
        current_status = _field(row["subscription_status"]) if "subscription_status" in row.keys() else SUBSCRIPTION_STATUS_PENDING
        current_operator = _field(row["subscription_operator_id"]) if "subscription_operator_id" in row.keys() else NULL_VALUE
        current_expires = _field(row["subscription_claim_expires_at"]) if "subscription_claim_expires_at" in row.keys() else NULL_VALUE
        if current_status not in {SUBSCRIPTION_STATUS_PENDING, SUBSCRIPTION_STATUS_FAILED, SUBSCRIPTION_STATUS_CLAIMED}:
            raise ValueError("账号当前不可领取")
        if current_status == SUBSCRIPTION_STATUS_CLAIMED:
            if current_operator == operator_id:
                pass
            elif current_operator == NULL_VALUE or not _subscription_claim_is_active(current_expires):
                pass
            else:
                raise ValueError("账号正在被其他操作员处理")
        db_manager.execute_sql(
            cursor,
            """
            UPDATE accounts
            SET subscription_status = ?,
                subscription_operator_id = ?,
                subscription_claimed_at = ?,
                subscription_claim_expires_at = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                SUBSCRIPTION_STATUS_CLAIMED,
                int(operator_user_id),
                now,
                expires_at,
                now,
                int(account_id),
            ),
        )
    account = get_account_db(int(account_id))
    if account is None:
        raise RuntimeError("账号领取后读取失败")
    return account


def mark_subscription_account_clicked_db(
    account_id: int,
    operator_user_id: int,
    *,
    note: str = "",
) -> dict[str, str]:
    return _update_subscription_account_status(
        account_id,
        operator_user_id,
        SUBSCRIPTION_STATUS_MARKED,
        note=note,
        clear_claim=False,
        marked_field="subscription_marked_at",
        allowed_current_statuses=(SUBSCRIPTION_STATUS_CLAIMED,),
        require_operator_claim=True,
    )


def mark_subscription_account_failed_db(
    account_id: int,
    operator_user_id: int,
    *,
    note: str = "",
) -> dict[str, str]:
    return _update_subscription_account_status(
        account_id,
        operator_user_id,
        SUBSCRIPTION_STATUS_FAILED,
        note=note,
        clear_claim=False,
        allowed_current_statuses=(SUBSCRIPTION_STATUS_CLAIMED,),
        require_operator_claim=True,
    )


def verify_subscription_account_db(account_id: int, *, note: str = "") -> dict[str, str]:
    return _update_subscription_account_status(
        account_id,
        None,
        SUBSCRIPTION_STATUS_VERIFIED,
        note=note,
        clear_claim=False,
        verified_field="subscription_verified_at",
        allowed_current_statuses=(SUBSCRIPTION_STATUS_MARKED, SUBSCRIPTION_STATUS_VERIFIED),
    )


def release_subscription_account_db(account_id: int, operator_user_id: int | None = None) -> dict[str, str]:
    return _update_subscription_account_status(
        account_id,
        operator_user_id,
        SUBSCRIPTION_STATUS_PENDING,
        note="",
        clear_claim=True,
        allowed_current_statuses=(SUBSCRIPTION_STATUS_CLAIMED,),
        require_operator_claim=operator_user_id is not None,
    )


def _update_subscription_account_status(
    account_id: int,
    operator_user_id: int | None,
    status: str,
    *,
    note: str = "",
    clear_claim: bool = False,
    marked_field: str = "",
    verified_field: str = "",
    allowed_current_statuses: tuple[str, ...] | None = None,
    require_operator_claim: bool = False,
) -> dict[str, str]:
    init_accounts_db()
    normalized_status = _normalize_subscription_status(status)
    now = utc_now()
    normalized_note = _field(note) if note else NULL_VALUE
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True, is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        existing = db_manager.execute_sql(
            cursor,
            """
            SELECT id, subscription_operator_id, subscription_status
            FROM accounts
            WHERE id = ?
            """,
            (int(account_id),),
        ).fetchone()
        if existing is None:
            raise KeyError(f"账号不存在: {account_id}")
        current_operator = _field(existing["subscription_operator_id"]) if "subscription_operator_id" in existing.keys() else NULL_VALUE
        current_status = _normalize_subscription_status(existing["subscription_status"]) if "subscription_status" in existing.keys() else SUBSCRIPTION_STATUS_PENDING
        if allowed_current_statuses is not None and current_status not in set(allowed_current_statuses):
            raise ValueError("账号当前状态不可执行该操作")
        if require_operator_claim and current_operator != _field(operator_user_id):
            raise ValueError("请先领取该账号")
        if operator_user_id is not None and current_operator not in {NULL_VALUE, _field(operator_user_id)}:
            raise ValueError("账号已分配给其他操作员")
        updates: list[str] = ["subscription_status = ?", "updated_at = ?"]
        params: list[Any] = [normalized_status, now]
        if operator_user_id is not None and not clear_claim:
            updates.append("subscription_operator_id = ?")
            params.append(int(operator_user_id))
        if clear_claim:
            updates.extend(
                [
                    "subscription_operator_id = NULL",
                    "subscription_claimed_at = NULL",
                    "subscription_claim_expires_at = NULL",
                ]
            )
        if marked_field:
            updates.append(f"{marked_field} = ?")
            params.append(now)
        if verified_field:
            updates.append(f"{verified_field} = ?")
            params.append(now)
        if normalized_note != NULL_VALUE:
            updates.append("subscription_note = ?")
            params.append(normalized_note)
        elif clear_claim:
            updates.append("subscription_note = ?")
            params.append(NULL_VALUE)
        db_manager.execute_sql(
            cursor,
            f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?",
            (*params, int(account_id)),
        )
    account = get_account_db(int(account_id))
    if account is None:
        raise RuntimeError("账号订阅状态更新后读取失败")
    return account


def ensure_bootstrap_admin_user(
    username: str = "admin",
    password: str | None = None,
    *,
    display_name: str = "管理员",
) -> dict[str, str] | None:
    """在没有任何后台用户时创建初始管理员账号。"""

    init_accounts_db()
    if any(
        user.get("role") == AUTH_ROLE_ADMIN and user.get("status") == AUTH_STATUS_ACTIVE
        for user in list_web_users()
    ):
        return None

    bootstrap_password = str(password or "").strip() or secrets.token_urlsafe(12)
    existing = get_web_user_by_username(username)
    if existing is not None:
        saved = update_web_user(
            int(existing["id"]),
            display_name=display_name or existing.get("display_name", ""),
            role=AUTH_ROLE_ADMIN,
            permissions=list(ADMIN_PERMISSIONS),
            status=AUTH_STATUS_ACTIVE,
            must_change_password=False,
        )
        set_web_user_password(int(saved["id"]), bootstrap_password, must_change_password=False)
    else:
        saved = create_web_user(
            username=username,
            password=bootstrap_password,
            role=AUTH_ROLE_ADMIN,
            display_name=display_name,
            permissions=list(ADMIN_PERMISSIONS),
            must_change_password=False,
        )
    return {"username": saved["username"], "password": bootstrap_password}


def count_web_users() -> int:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn() as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(cursor, "SELECT COUNT(*) FROM web_users").fetchone()
    return int(row[0] if row is not None else 0)


def list_web_users() -> list[dict[str, str]]:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        rows = db_manager.execute_sql(
            cursor,
            """
            SELECT id, username, display_name, role, permissions_json, status, must_change_password,
                   created_at, updated_at, last_login_at, last_login_ip, last_login_user_agent
            FROM web_users
            ORDER BY CASE WHEN role = ? THEN 0 ELSE 1 END, id ASC
            """,
            (AUTH_ROLE_ADMIN,),
        ).fetchall()
    return [_web_user_from_row(row) for row in rows]


def get_web_user_by_id(user_id: int) -> dict[str, str] | None:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(
            cursor,
            """
            SELECT id, username, display_name, role, permissions_json, status, must_change_password,
                   created_at, updated_at, last_login_at, last_login_ip, last_login_user_agent
            FROM web_users
            WHERE id = ?
            """,
            (int(user_id),),
        ).fetchone()
    return _web_user_from_row(row) if row is not None else None


def get_web_user_by_username(username: str) -> dict[str, str] | None:
    normalized = _field(username).lower()
    if normalized == NULL_VALUE:
        return None
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(
            cursor,
            """
            SELECT id, username, display_name, role, permissions_json, status, must_change_password,
                   created_at, updated_at, last_login_at, last_login_ip, last_login_user_agent
            FROM web_users
            WHERE username = ?
            """,
            (normalized,),
        ).fetchone()
    return _web_user_from_row(row) if row is not None else None


def create_web_user(
    *,
    username: str,
    password: str,
    role: str = AUTH_ROLE_OPERATOR,
    display_name: str = "",
    permissions: list[str] | tuple[str, ...] | None = None,
    status: str = AUTH_STATUS_ACTIVE,
    must_change_password: bool = False,
) -> dict[str, str]:
    init_accounts_db()
    normalized_username = _field(username).lower()
    if normalized_username == NULL_VALUE:
        raise ValueError("用户名不能为空")
    normalized_role = _normalize_auth_role(role)
    normalized_status = _normalize_auth_status(status)
    permissions_json = _permissions_json(permissions, normalized_role)
    password_hash = _hash_password(password)
    now = utc_now()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True, is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        existing = db_manager.execute_sql(
            cursor,
            "SELECT id FROM web_users WHERE username = ?",
            (normalized_username,),
        ).fetchone()
        if existing is not None:
            raise ValueError(f"用户名已存在: {normalized_username}")
        db_manager.execute_sql(
            cursor,
            """
            INSERT INTO web_users (
                username, display_name, role, permissions_json, password_hash, status,
                must_change_password, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized_username,
                str(display_name or "").strip(),
                normalized_role,
                permissions_json,
                password_hash,
                normalized_status,
                1 if must_change_password else 0,
                now,
                now,
            ),
        )
        user_id = int(cursor.lastrowid)
    user = get_web_user_by_id(user_id)
    if user is None:
        raise RuntimeError("用户创建后读取失败")
    return user


def update_web_user(
    user_id: int,
    *,
    display_name: str | None = None,
    role: str | None = None,
    permissions: list[str] | tuple[str, ...] | None = None,
    status: str | None = None,
    must_change_password: bool | None = None,
) -> dict[str, str]:
    init_accounts_db()
    current = get_web_user_by_id(user_id)
    if current is None:
        raise KeyError(f"用户不存在: {user_id}")
    next_display_name = current.get("display_name", "")
    next_role = current.get("role", AUTH_ROLE_OPERATOR)
    next_status = current.get("status", AUTH_STATUS_ACTIVE)
    next_permissions = _load_permissions(current.get("permissions_json"))
    next_must_change = _bool_from_value(current.get("must_change_password"))
    if display_name is not None:
        next_display_name = str(display_name or "").strip()
    if role is not None:
        next_role = _normalize_auth_role(role)
    if status is not None:
        next_status = _normalize_auth_status(status)
    if permissions is not None:
        next_permissions = _normalize_permissions(permissions, next_role)
    else:
        next_permissions = _normalize_permissions(next_permissions, next_role)
    if must_change_password is not None:
        next_must_change = bool(must_change_password)
    permissions_json = json.dumps(next_permissions, ensure_ascii=False, separators=(",", ":"))
    now = utc_now()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True, is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            """
            UPDATE web_users
            SET display_name = ?, role = ?, permissions_json = ?, status = ?,
                must_change_password = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                next_display_name,
                next_role,
                permissions_json,
                next_status,
                1 if next_must_change else 0,
                now,
                int(user_id),
            ),
        )
    user = get_web_user_by_id(user_id)
    if user is None:
        raise RuntimeError("用户更新后读取失败")
    return user


def set_web_user_password(user_id: int, password: str, *, must_change_password: bool = False) -> dict[str, str]:
    init_accounts_db()
    password_hash = _hash_password(password)
    now = utc_now()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True, is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            """
            UPDATE web_users
            SET password_hash = ?, must_change_password = ?, updated_at = ?
            WHERE id = ?
            """,
            (password_hash, 1 if must_change_password else 0, now, int(user_id)),
        )
        if cursor.rowcount <= 0:
            raise KeyError(f"用户不存在: {user_id}")
    user = get_web_user_by_id(user_id)
    if user is None:
        raise RuntimeError("用户密码更新后读取失败")
    return user


def authenticate_web_user(
    username: str,
    password: str,
    *,
    ip: str = "",
    user_agent: str = "",
) -> dict[str, str] | None:
    user = get_web_user_by_username(username)
    if user is None or user.get("status") != AUTH_STATUS_ACTIVE:
        return None
    stored = _get_web_user_password_hash(user.get("id"))
    if not stored or not _verify_password(password, stored):
        return None
    now = utc_now()
    db_manager = _db_manager()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            """
            UPDATE web_users
            SET last_login_at = ?, last_login_ip = ?, last_login_user_agent = ?, updated_at = ?
            WHERE id = ?
            """,
            (now, _field(ip), _field(user_agent), now, int(user["id"])),
        )
    refreshed = get_web_user_by_id(int(user["id"]))
    return refreshed


def create_web_session(
    user_id: int,
    *,
    ip: str = "",
    user_agent: str = "",
    ttl_seconds: int = WEB_SESSION_TTL_SECONDS,
) -> dict[str, str]:
    init_accounts_db()
    token = secrets.token_urlsafe(32)
    token_hash = _web_session_token_hash(token)
    now = utc_now()
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=max(60, int(ttl_seconds)))).isoformat()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True, is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            """
            INSERT INTO web_sessions (
                user_id, session_token_hash, expires_at, created_at, last_seen_at, revoked_at, ip, user_agent
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (int(user_id), token_hash, expires_at, now, now, _field(ip), _field(user_agent)),
        )
        session_id = int(cursor.lastrowid)
    return {"session_id": str(session_id), "token": token, "expires_at": expires_at}


def resolve_web_session(token: str) -> dict[str, str] | None:
    normalized = _field(token)
    if normalized == NULL_VALUE:
        return None
    token_hash = _web_session_token_hash(normalized)
    now = utc_now()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True, is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(
            cursor,
            """
            SELECT id, user_id, session_token_hash, expires_at, revoked_at
            FROM web_sessions
            WHERE session_token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            return None
        if _field(row["revoked_at"]) != NULL_VALUE:
            return None
        expires_at = _field(row["expires_at"])
        try:
            if datetime.fromisoformat(expires_at) <= datetime.now(timezone.utc):
                return None
        except Exception:
            return None
        user = get_web_user_by_id(int(row["user_id"]))
        if user is None:
            return None
        if user.get("status") != AUTH_STATUS_ACTIVE:
            return None
        db_manager.execute_sql(
            cursor,
            "UPDATE web_sessions SET last_seen_at = ? WHERE id = ?",
            (now, int(row["id"])),
        )
    return {
        "session_id": str(row["id"]),
        "user_id": str(row["user_id"]),
        "expires_at": expires_at,
        **user,
    }


def revoke_web_session(token: str) -> bool:
    normalized = _field(token)
    if normalized == NULL_VALUE:
        return False
    token_hash = _web_session_token_hash(normalized)
    now = utc_now()
    db_manager = _db_manager()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            "UPDATE web_sessions SET revoked_at = ? WHERE session_token_hash = ? AND revoked_at IS NULL",
            (now, token_hash),
        )
        return cursor.rowcount > 0


def revoke_web_sessions_for_user(user_id: int) -> int:
    now = utc_now()
    db_manager = _db_manager()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            "UPDATE web_sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now, int(user_id)),
        )
        return int(cursor.rowcount or 0)


def record_audit_log(
    actor_user_id: int | None,
    action: str,
    *,
    target_type: str = "",
    target_id: str = "",
    detail: dict[str, Any] | None = None,
    ip: str = "",
    user_agent: str = "",
) -> None:
    init_accounts_db()
    db_manager = _db_manager()
    now = utc_now()
    detail_json = _json_blob(detail if detail is not None else None)
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            """
            INSERT INTO audit_logs (
                actor_user_id, action, target_type, target_id, detail_json, ip, user_agent, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(actor_user_id) if actor_user_id is not None else None,
                _field(action),
                _field(target_type),
                _field(target_id),
                detail_json,
                _field(ip),
                _field(user_agent),
                now,
            ),
        )


def import_compact_accounts_to_db(path: Path) -> None:
    init_accounts_db()
    for account in load_compact_accounts(path):
        fields = [
            account["email"],
            account["password"],
            account.get("subscription_type", NULL_VALUE),
            account.get("refresh_token", NULL_VALUE),
            account.get("session", NULL_VALUE),
        ]
        _upsert_account_db(fields, "import")


def import_sessions_to_db(path: Path) -> None:
    init_accounts_db()
    sessions = _read_json_obj(path)
    for key, record in sessions.items():
        if not isinstance(record, dict):
            continue
        email = str(record.get("email") or key or "").strip().lower()
        password = str(record.get("password") or "").strip()
        if not email or not password or password.lower() == NULL_VALUE:
            continue
        fields = _compact_account_fields(email, password, record)
        _upsert_account_db(fields, "import")


def export_accounts_db_to_txt(output: Path) -> None:
    """Legacy helper: write the compact account export to a text file."""

    with _file_write_lock:
        text = build_accounts_db_txt()
        if not text:
            return
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")


def build_accounts_db_txt() -> str:
    accounts = load_accounts_db()
    if not accounts:
        return ""
    lines = [
        "----".join(
            [
                account["email"],
                account["password"],
                account["subscription_type"],
                account["refresh_token"],
                account["session"],
            ]
        )
        for account in accounts
    ]
    return "\n".join(lines) + "\n"


def build_tokens_db_jsonl() -> str:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        rows = db_manager.execute_sql(
            cursor,
            """
            SELECT auth_token_json
            FROM accounts
            WHERE auth_token_json IS NOT NULL AND auth_token_json != 'null' AND auth_token_json != ''
            ORDER BY id ASC
            """,
        ).fetchall()
    lines: list[str] = []
    for row in rows:
        blob = _field(row["auth_token_json"])
        if not _has_session_blob(blob):
            continue
        lines.append(blob)
    return "\n".join(lines) + ("\n" if lines else "")


def build_checkout_db_jsonl() -> str:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        rows = db_manager.execute_sql(
            cursor,
            """
            SELECT email, checkout_json
            FROM accounts
            WHERE checkout_json IS NOT NULL AND checkout_json != 'null' AND checkout_json != ''
            ORDER BY id ASC
            """,
        ).fetchall()
    lines: list[str] = []
    for row in rows:
        blob = _field(row["checkout_json"])
        if not _has_session_blob(blob):
            continue
        lines.append(blob)
    return "\n".join(lines) + ("\n" if lines else "")


def load_compact_accounts(path: Path) -> list[dict[str, str]]:
    normalize_compact_accounts_txt(path)
    accounts: list[dict[str, str]] = []
    for line in _read_lines(path):
        fields = _parse_compact_account_line(line)
        if fields is None:
            continue
        accounts.append(
            {
                "email": fields[0],
                "password": fields[1],
                "subscription_type": fields[2],
                "refresh_token": fields[3],
                "session": fields[4],
            }
        )
    return accounts


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_account(email: str, password: str, token_data: dict[str, Any]) -> None:
    """保存授权结果到数据库。"""

    save_authorization_token_db(email, password, token_data)


def save_login_session_db(email: str, password: str, session_data: dict[str, Any]) -> dict[str, str] | None:
    """保存登录会话快照到数据库。"""

    init_accounts_db()
    payload = {
        **session_data,
        "email": email,
        "password": password,
        "updated_at": utc_now(),
    }
    snapshot = _json_blob(payload)
    fields = _compact_account_fields(email, password, session_data)
    _upsert_account_db(fields, "login")

    normalized_email = _field(email).lower()
    if normalized_email == NULL_VALUE:
        return None

    db_manager = _db_manager()
    now = utc_now()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            "UPDATE accounts SET login_session_json = ?, updated_at = ?, last_login_at = ? WHERE email = ?",
            (snapshot, now, now, normalized_email),
        )
        if cursor.rowcount <= 0:
            return None
    return _get_account_db_by_email(normalized_email)


def save_authorization_token_db(email: str, password: str, token_data: dict[str, Any]) -> dict[str, str] | None:
    """保存授权结果到数据库。"""

    init_accounts_db()
    fields = _compact_account_fields(email, password, token_data)
    _upsert_account_db(fields, "authorize")

    normalized_email = _field(email).lower()
    if normalized_email == NULL_VALUE:
        return None

    record = {
        "email": token_data.get("email") or email,
        "password": password,
        "token_data": token_data,
        "created_at": utc_now(),
    }
    blob = _json_blob(record)
    db_manager = _db_manager()
    now = utc_now()
    with db_manager.get_db_conn(is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        db_manager.execute_sql(
            cursor,
            "UPDATE accounts SET auth_token_json = ?, updated_at = ?, last_authorized_at = ? WHERE email = ?",
            (blob, now, now, normalized_email),
        )
        if cursor.rowcount <= 0:
            return None
    return _get_account_db_by_email(normalized_email)


def try_load_login_session_db(email: str) -> dict[str, Any] | None:
    account = _get_account_db_by_email(email)
    if account is None:
        return None
    blob = account.get("login_session")
    if not _has_session_blob(blob):
        return None
    session = _load_json_blob(blob)
    if not isinstance(session, dict):
        return None
    cookies = session.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return None
    return session


def save_credentials_txt(output: Path, email: str, password: str) -> None:
    save_compact_account(output, email, password, {})


def save_credentials_rt_txt(output: Path, email: str, password: str, refresh_token: str) -> None:
    save_compact_account(output, email, password, {"refresh_token": refresh_token})


def save_compact_account(output: Path, email: str, password: str, token_data: dict[str, Any] | None = None) -> None:
    """写入账号紧凑格式：账号----密码----订阅类型----rt----session。"""

    fields = _compact_account_fields(email, password, token_data or {})
    _upsert_compact_account(output, fields)


def merge_legacy_rt_txt(accounts_output: Path, rt_output: Path) -> None:
    """Legacy helper: merge refresh tokens from old compact account files."""

    normalize_compact_accounts_txt(accounts_output)
    if not rt_output.exists() or not rt_output.is_file():
        return
    for line in _read_lines(rt_output):
        fields = _parse_compact_account_line(line)
        if fields is None:
            continue
        _upsert_compact_account(accounts_output, fields)
    normalize_compact_accounts_txt(accounts_output)


def normalize_compact_accounts_txt(output: Path) -> None:
    """把旧账号行统一整理为账号----密码----订阅类型----rt----session。"""

    with _file_write_lock:
        lines = _read_lines(output)
        if not lines:
            return
        normalized: list[str] = []
        email_indexes: dict[str, int] = {}
        changed = False
        for line in lines:
            fields = _parse_compact_account_line(line)
            if fields is None:
                normalized.append(line)
                continue
            compact = "----".join(fields)
            if compact != line:
                changed = True
            email = fields[0].lower()
            if email in email_indexes:
                index = email_indexes[email]
                old_fields = _parse_compact_account_line(normalized[index]) or fields
                normalized[index] = "----".join(_merge_compact_fields(old_fields, fields))
                changed = True
                continue
            email_indexes[email] = len(normalized)
            normalized.append(compact)
        if changed:
            output.write_text("\n".join(normalized) + "\n", encoding="utf-8")


def _compact_account_fields(email: str, password: str, token_data: dict[str, Any]) -> list[str]:
    return [
        _field(email.lower()),
        _field(password),
        _field(_extract_subscription_type(token_data)),
        _field(_extract_refresh_token(token_data)),
        _field(_extract_session(token_data)),
    ]


def _upsert_compact_account(output: Path, fields: list[str]) -> None:
    with _file_write_lock:
        output.parent.mkdir(parents=True, exist_ok=True)
        target = fields[0].lower()
        lines = _read_lines(output)
        updated: list[str] = []
        replaced = False
        for line in lines:
            current = _parse_compact_account_line(line)
            if current is None or current[0].lower() != target:
                updated.append(line)
                continue
            if not replaced:
                updated.append("----".join(_merge_compact_fields(current, fields)))
                replaced = True
        if not replaced:
            updated.append("----".join(fields))
        output.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _upsert_account_db(fields: list[str], source: str) -> None:
    normalized = _normalize_account_fields(fields)
    if normalized is None:
        return

    db_manager = _db_manager()
    now = utc_now()
    with db_manager.get_db_conn(as_dict=True, is_write=True) as conn:
        cursor = db_manager.get_cursor(conn)
        existing = db_manager.execute_sql(
            cursor,
            """
            SELECT email, password, subscription_type, refresh_token, session_json,
                   status, created_at, last_login_at, last_authorized_at
            FROM accounts
            WHERE email = ?
            """,
            (normalized[0],),
        ).fetchone()
        if existing is None:
            last_login_at = now if source in {"register", "login"} else None
            last_authorized_at = now if source == "authorize" else None
            db_manager.execute_sql(
                cursor,
                """
                INSERT INTO accounts (
                    email, password, subscription_type, refresh_token, session_json,
                    stock_status, status, created_at, updated_at, last_login_at, last_authorized_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *normalized,
                    STOCK_STATUS_IN,
                    ACCOUNT_STATUS_ACTIVE,
                    now,
                    now,
                    last_login_at,
                    last_authorized_at,
                ),
            )
            return

        old_fields = [
            _field(existing["email"]).lower(),
            _field(existing["password"]),
            _field(existing["subscription_type"]),
            _field(existing["refresh_token"]),
            _session_field(existing["session_json"]),
        ]
        merged = _merge_compact_fields(old_fields, normalized)
        last_login_at = existing["last_login_at"]
        last_authorized_at = existing["last_authorized_at"]
        if source in {"register", "login"}:
            last_login_at = now
        if source == "authorize":
            last_authorized_at = now

        db_manager.execute_sql(
            cursor,
            """
            UPDATE accounts
            SET email = ?,
                password = ?,
                subscription_type = ?,
                refresh_token = ?,
                session_json = ?,
                status = ?,
                updated_at = ?,
                last_login_at = ?,
                last_authorized_at = ?
            WHERE email = ?
            """,
            (
                *merged,
                _field(existing["status"]) if _field(existing["status"]) != NULL_VALUE else ACCOUNT_STATUS_ACTIVE,
                now,
                last_login_at,
                last_authorized_at,
                old_fields[0],
            ),
        )


def _normalize_account_fields(fields: list[str]) -> list[str] | None:
    if len(fields) < ACCOUNT_FIELD_COUNT:
        return None
    normalized = [_field(fields[index]) for index in range(ACCOUNT_FIELD_COUNT)]
    normalized[0] = normalized[0].lower()
    normalized[4] = _session_field(normalized[4])
    if normalized[2] == NULL_VALUE:
        normalized[2] = _field(_subscription_from_session_field(normalized[4]))
    if normalized[0] == NULL_VALUE or normalized[1] == NULL_VALUE:
        return None
    return normalized


def _account_from_row(row: Any) -> dict[str, str]:
    keys = row.keys()
    account = {
        "email": _field(row["email"]).lower(),
        "password": _field(row["password"]),
        "subscription_type": _field(row["subscription_type"]),
        "refresh_token": _field(row["refresh_token"]),
        "session": _session_field(row["session_json"]),
        "checkout_url": _field(row["checkout_url"]) if "checkout_url" in keys else NULL_VALUE,
        "login_session": _field(row["login_session_json"]) if "login_session_json" in keys else NULL_VALUE,
        "auth_token": _field(row["auth_token_json"]) if "auth_token_json" in keys else NULL_VALUE,
        "checkout": _field(row["checkout_json"]) if "checkout_json" in keys else NULL_VALUE,
        "subscription_status": _normalize_subscription_status(row["subscription_status"]) if "subscription_status" in keys else SUBSCRIPTION_STATUS_PENDING,
        "subscription_operator_id": _field(row["subscription_operator_id"]) if "subscription_operator_id" in keys else NULL_VALUE,
        "subscription_claimed_at": _field(row["subscription_claimed_at"]) if "subscription_claimed_at" in keys else NULL_VALUE,
        "subscription_claim_expires_at": _field(row["subscription_claim_expires_at"]) if "subscription_claim_expires_at" in keys else NULL_VALUE,
        "subscription_marked_at": _field(row["subscription_marked_at"]) if "subscription_marked_at" in keys else NULL_VALUE,
        "subscription_verified_at": _field(row["subscription_verified_at"]) if "subscription_verified_at" in keys else NULL_VALUE,
        "subscription_note": _field(row["subscription_note"]) if "subscription_note" in keys else NULL_VALUE,
        "stock_status": _normalize_stock_status(row["stock_status"]) if "stock_status" in keys else STOCK_STATUS_IN,
        "status": _field(row["status"]),
        "created_at": _field(row["created_at"]),
        "updated_at": _field(row["updated_at"]),
        "last_login_at": _field(row["last_login_at"]),
        "last_authorized_at": _field(row["last_authorized_at"]),
    }
    if "id" in keys:
        account["id"] = str(row["id"])
    return account


def _web_user_from_row(row: Any) -> dict[str, str]:
    permissions = _load_permissions(row["permissions_json"])
    return {
        "id": str(row["id"]),
        "username": _field(row["username"]).lower(),
        "display_name": str(row["display_name"] or "").strip(),
        "role": _normalize_auth_role(row["role"]),
        "permissions": permissions,
        "permissions_json": json.dumps(permissions, ensure_ascii=False, separators=(",", ":")),
        "status": _normalize_auth_status(row["status"]),
        "must_change_password": "true" if _bool_from_value(row["must_change_password"]) else "false",
        "created_at": _field(row["created_at"]),
        "updated_at": _field(row["updated_at"]),
        "last_login_at": _field(row["last_login_at"]),
        "last_login_ip": _field(row["last_login_ip"]),
        "last_login_user_agent": _field(row["last_login_user_agent"]),
    }


def _normalize_auth_role(role: object) -> str:
    text = str(role or "").strip().lower()
    return text if text in AUTH_ROLES else AUTH_ROLE_OPERATOR


def _normalize_auth_status(status: object) -> str:
    text = str(status or "").strip().lower()
    return text if text in AUTH_STATUSES else AUTH_STATUS_ACTIVE


def _permissions_json(permissions: list[str] | tuple[str, ...] | None, role: str) -> str:
    normalized = _normalize_permissions(permissions, role)
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _normalize_permissions(permissions: list[str] | tuple[str, ...] | None, role: str) -> list[str]:
    if role == AUTH_ROLE_ADMIN:
        return list(ADMIN_PERMISSIONS)
    values = list(DEFAULT_OPERATOR_PERMISSIONS if permissions is None else permissions)
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _load_permissions(raw: object) -> list[str]:
    if isinstance(raw, list):
        return _normalize_permissions([str(item) for item in raw], AUTH_ROLE_OPERATOR)
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, list):
        return [str(item) for item in data if str(item or "").strip()]
    return []


def _bool_from_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def _get_web_user_password_hash(user_id: object) -> str:
    try:
        normalized_id = int(str(user_id))
    except Exception:
        return ""
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn() as conn:
        cursor = db_manager.get_cursor(conn)
        row = db_manager.execute_sql(
            cursor,
            "SELECT password_hash FROM web_users WHERE id = ?",
            (normalized_id,),
        ).fetchone()
    return str(row[0] or "") if row is not None else ""


def _hash_password(password: str) -> str:
    text = str(password or "")
    if len(text) < 6:
        raise ValueError("密码至少需要 6 位")
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        text.encode("utf-8"),
        bytes.fromhex(salt),
        PASSWORD_HASH_ITERATIONS,
    ).hex()
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${salt}${digest}"


def _verify_password(password: str, stored_hash: str) -> bool:
    try:
        algo, raw_iterations, salt, expected = str(stored_hash or "").split("$", 3)
        iterations = int(raw_iterations)
    except Exception:
        return False
    if algo != "pbkdf2_sha256" or not salt or not expected:
        return False
    try:
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            str(password or "").encode("utf-8"),
            bytes.fromhex(salt),
            iterations,
        ).hex()
    except Exception:
        return False
    return hmac.compare_digest(actual, expected)


def _web_session_token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _normalize_stock_status(value: object) -> str:
    text = str(value or "").strip()
    if text in STOCK_STATUSES:
        return text
    if text.lower() in {"out", "sold", "used", "1", "true", "yes"}:
        return STOCK_STATUS_OUT
    return STOCK_STATUS_IN


def _normalize_subscription_status(value: object) -> str:
    text = str(value or "").strip()
    if text in SUBSCRIPTION_STATUSES:
        return text
    return SUBSCRIPTION_STATUS_PENDING


def _subscription_claim_is_active(value: object) -> bool:
    text = _field(value)
    if text == NULL_VALUE:
        return False
    try:
        expires_at = datetime.fromisoformat(text)
    except Exception:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > datetime.now(timezone.utc)


def _stock_status_counts(rows: list[Any]) -> dict[str, int]:
    counts = {STOCK_STATUS_IN: 0, STOCK_STATUS_OUT: 0}
    for status, count in rows:
        normalized = _normalize_stock_status(status)
        counts[normalized] = counts.get(normalized, 0) + int(count)
    return counts


def _merge_compact_fields(old: list[str], new: list[str]) -> list[str]:
    merged = old[:ACCOUNT_FIELD_COUNT]
    for index, value in enumerate(new[:ACCOUNT_FIELD_COUNT]):
        if value != NULL_VALUE:
            merged[index] = value
    return merged


def _parse_compact_account_line(line: str) -> list[str] | None:
    parts = line.rstrip("\n").split("----")
    if len(parts) < 2:
        return None
    email = _field(parts[0]).lower()
    password = _field(parts[1])
    if email == NULL_VALUE or password == NULL_VALUE:
        return None
    if len(parts) >= ACCOUNT_FIELD_COUNT:
        fields = [_field(parts[index]) for index in range(ACCOUNT_FIELD_COUNT)]
        fields[4] = _session_field(fields[4])
        return fields
    if len(parts) >= 3:
        return [email, password, NULL_VALUE, _field(parts[2]), NULL_VALUE]
    return [email, password, NULL_VALUE, NULL_VALUE, NULL_VALUE]


def _ensure_accounts_column(cursor: Any, name: str, definition: str) -> None:
    columns = {str(row[1]) for row in cursor.execute("PRAGMA table_info(accounts)").fetchall()}
    if name not in columns:
        cursor.execute(f"ALTER TABLE accounts ADD COLUMN {name} {definition}")


def _drop_accounts_column(cursor: Any, name: str) -> None:
    columns = {str(row[1]) for row in cursor.execute("PRAGMA table_info(accounts)").fetchall()}
    if name not in columns:
        return
    try:
        cursor.execute(f"ALTER TABLE accounts DROP COLUMN {name}")
    except Exception:
        pass


def _read_lines(path: Path) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="ignore").splitlines()


def _field(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == NULL_VALUE:
        return NULL_VALUE
    return text.replace("\r", "").replace("\n", "").replace("----", "__")


def _session_field(value: object) -> str:
    text = _field(value)
    if text == NULL_VALUE:
        return NULL_VALUE
    if text.startswith(("{", "[")):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return NULL_VALUE
        return _session_json(data)
    return NULL_VALUE


def _extract_refresh_token(token_data: dict[str, Any]) -> str:
    return str(token_data.get("refresh_token") or token_data.get("refreshToken") or "")


def _extract_session(token_data: dict[str, Any]) -> str:
    session = token_data.get("chatgpt_session")
    if isinstance(session, dict):
        return _session_json(session)
    if isinstance(token_data.get("data"), dict) and ("accessToken" in token_data["data"] or "user" in token_data["data"]):
        return _session_json(token_data)
    return ""


def _session_json(session: object) -> str:
    if isinstance(session, dict) and isinstance(session.get("data"), dict):
        session = session["data"]
    if not isinstance(session, (dict, list)):
        return NULL_VALUE
    return json.dumps(session, ensure_ascii=False, separators=(",", ":"))


def _subscription_from_session_field(session_field: str) -> str:
    if session_field == NULL_VALUE:
        return ""
    try:
        session = json.loads(session_field)
    except json.JSONDecodeError:
        return ""
    if not isinstance(session, (dict, list)):
        return ""
    return _extract_subscription_type({"chatgpt_session": {"data": session}})


def _extract_subscription_type(token_data: dict[str, Any]) -> str:
    key_names = {
        "account_type",
        "accountType",
        "subscription_type",
        "subscriptionType",
        "subscription_plan",
        "subscriptionPlan",
        "chatgpt_plan_type",
        "plan_type",
        "planType",
        "plan_name",
        "planName",
        "account_plan",
        "accountPlan",
        "billing_plan",
        "billingPlan",
        "product_name",
        "productName",
        "sku",
    }
    found = _find_first_key(token_data, key_names)
    if found is not None:
        return str(found)
    session_data = _chatgpt_session_data(token_data)
    if isinstance(session_data, dict):
        found = _find_first_key(session_data, key_names)
        if found is not None:
            return str(found)
    return ""


def _chatgpt_session_data(token_data: dict[str, Any]) -> object:
    session = token_data.get("chatgpt_session")
    if not isinstance(session, dict):
        return None
    return session.get("data")


def _find_first_key(value: object, key_names: set[str]) -> object:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in key_names and isinstance(item, (str, int, float, bool)) and str(item).strip():
                return item
        for item in value.values():
            found = _find_first_key(item, key_names)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_first_key(item, key_names)
            if found is not None:
                return found
    return None


def dump_session_cookies(session: Any) -> list[dict[str, Any]]:
    cookies = []
    for cookie in session.cookies.jar:
        cookies.append(
            {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "secure": bool(cookie.secure),
                "expires": cookie.expires,
            }
        )
    return cookies


def apply_session_cookies(session: Any, cookies: list[dict[str, Any]]) -> None:
    session.cookies.clear()
    for cookie in cookies:
        name = str(cookie.get("name") or "")
        value = str(cookie.get("value") or "")
        if not name:
            continue
        session.cookies.set(
            name,
            value,
            domain=str(cookie.get("domain") or ""),
            path=str(cookie.get("path") or "/"),
            secure=bool(cookie.get("secure") or False),
        )


def save_login_session(path: Path, email: str, password: str, session_data: dict[str, Any]) -> None:
    del path
    save_login_session_db(email, password, session_data)


def load_login_session(path: Path, email: str) -> dict[str, Any]:
    del path
    record = try_load_login_session_db(email)
    if not isinstance(record, dict):
        raise RuntimeError(f"未找到登录会话，请先执行 login 模式: {email}")
    return record


def try_load_login_session(path: Path, email: str) -> dict[str, Any] | None:
    del path
    return try_load_login_session_db(email)


def _read_json_obj(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"会话文件不是有效 JSON: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"会话文件格式错误: {path}")
    return data


def _json_blob(value: object) -> str:
    if value is None:
        return NULL_VALUE
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return NULL_VALUE


def _load_json_blob(value: object) -> object:
    text = _field(value)
    if text == NULL_VALUE:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _has_session_blob(value: object) -> bool:
    text = _field(value)
    if text == NULL_VALUE:
        return False
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return False
    return isinstance(loaded, (dict, list))


def _db_manager() -> Any:
    from utils import db_manager

    return db_manager
