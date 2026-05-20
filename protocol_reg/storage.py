from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NULL_VALUE = "null"
ACCOUNT_FIELD_COUNT = 5


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
                status TEXT DEFAULT 'active',
                source TEXT DEFAULT 'manual',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT,
                last_authorized_at TEXT
            )
            """,
        )
        db_manager.execute_sql(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status)",
        )
        db_manager.execute_sql(
            cursor,
            "CREATE INDEX IF NOT EXISTS idx_accounts_updated_at ON accounts(updated_at)",
        )


def sync_account_storage(accounts_output: Path, session_file: Path) -> None:
    """启动时把 TXT 和会话文件导入数据库，再从数据库导出兼容 TXT。"""

    init_accounts_db()
    import_compact_accounts_to_db(accounts_output)
    import_sessions_to_db(session_file)
    export_accounts_db_to_txt(accounts_output)


def save_account_storage(
    accounts_output: Path,
    email: str,
    password: str,
    token_data: dict[str, Any] | None = None,
    *,
    source: str = "manual",
) -> None:
    """把账号写入数据库，并同步导出 accounts.txt。"""

    init_accounts_db()
    fields = _compact_account_fields(email, password, token_data or {})
    _upsert_account_db(fields, source)
    export_accounts_db_to_txt(accounts_output)


def load_account_records(accounts_output: Path | None = None) -> list[dict[str, str]]:
    """读取数据库账号；数据库为空时可回退读取 TXT。"""

    accounts = load_accounts_db()
    if accounts or accounts_output is None:
        return accounts
    return load_compact_accounts(accounts_output)


def load_accounts_db() -> list[dict[str, str]]:
    init_accounts_db()
    db_manager = _db_manager()
    with db_manager.get_db_conn(as_dict=True) as conn:
        cursor = db_manager.get_cursor(conn)
        rows = db_manager.execute_sql(
            cursor,
            """
            SELECT email, password, subscription_type, refresh_token, session_json,
                   status, source, created_at, updated_at, last_login_at, last_authorized_at
            FROM accounts
            ORDER BY id ASC
            """,
        ).fetchall()
    return [_account_from_row(row) for row in rows]


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
    accounts = load_accounts_db()
    if not accounts:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
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
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


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


def sync_compact_accounts_from_sessions(accounts_output: Path, session_file: Path) -> None:
    sync_account_storage(accounts_output, session_file)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_account(output: Path, email: str, password: str, token_data: dict[str, Any]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "email": token_data.get("email") or email,
        "password": password,
        "token_data": token_data,
        "created_at": utc_now(),
    }
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def save_credentials_txt(output: Path, email: str, password: str) -> None:
    save_compact_account(output, email, password, {})


def save_credentials_rt_txt(output: Path, email: str, password: str, refresh_token: str) -> None:
    save_compact_account(output, email, password, {"refresh_token": refresh_token})


def save_compact_account(output: Path, email: str, password: str, token_data: dict[str, Any] | None = None) -> None:
    """写入账号紧凑格式：账号----密码----订阅类型----rt----session。"""

    fields = _compact_account_fields(email, password, token_data or {})
    _upsert_compact_account(output, fields)


def merge_legacy_rt_txt(accounts_output: Path, rt_output: Path) -> None:
    """把旧 accounts_rt.txt 的账号----密码----rt 合并进 accounts.txt。"""

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
                   status, source, created_at, last_login_at, last_authorized_at
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
                    status, source, created_at, updated_at, last_login_at, last_authorized_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    *normalized,
                    "active",
                    _field(source) if _field(source) != NULL_VALUE else "manual",
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
                source = ?,
                updated_at = ?,
                last_login_at = ?,
                last_authorized_at = ?
            WHERE email = ?
            """,
            (
                *merged,
                _field(existing["status"]) if _field(existing["status"]) != NULL_VALUE else "active",
                _merge_source(str(existing["source"] or ""), source),
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
    return {
        "email": _field(row["email"]).lower(),
        "password": _field(row["password"]),
        "subscription_type": _field(row["subscription_type"]),
        "refresh_token": _field(row["refresh_token"]),
        "session": _session_field(row["session_json"]),
        "status": _field(row["status"]),
        "source": _field(row["source"]),
        "created_at": _field(row["created_at"]),
        "updated_at": _field(row["updated_at"]),
        "last_login_at": _field(row["last_login_at"]),
        "last_authorized_at": _field(row["last_authorized_at"]),
    }


def _merge_source(old_source: str, new_source: str) -> str:
    old_value = _field(old_source)
    new_value = _field(new_source)
    if new_value == NULL_VALUE:
        return old_value if old_value != NULL_VALUE else "manual"
    if new_value == "import" and old_value not in {NULL_VALUE, "manual", "import"}:
        return old_value
    return new_value


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
    path.parent.mkdir(parents=True, exist_ok=True)
    data = _read_json_obj(path)
    key = email.strip().lower()
    data[key] = {
        **session_data,
        "email": email,
        "password": password,
        "updated_at": utc_now(),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_login_session(path: Path, email: str) -> dict[str, Any]:
    data = _read_json_obj(path)
    record = data.get(email.strip().lower())
    if not isinstance(record, dict):
        raise RuntimeError(f"未找到登录会话，请先执行 login 模式: {email}")
    cookies = record.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise RuntimeError(f"登录会话没有可用 cookies，请重新执行 login 模式: {email}")
    return record


def try_load_login_session(path: Path, email: str) -> dict[str, Any] | None:
    data = _read_json_obj(path)
    record = data.get(email.strip().lower())
    if not isinstance(record, dict):
        return None
    cookies = record.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return None
    return record


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


def _db_manager() -> Any:
    from utils import db_manager

    return db_manager
