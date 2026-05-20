from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

NULL_VALUE = "null"
ACCOUNT_FIELD_COUNT = 5


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
                "access_token": fields[4],
            }
        )
    return accounts


def sync_compact_accounts_from_sessions(accounts_output: Path, session_file: Path) -> None:
    sessions = _read_json_obj(session_file)
    for key, record in sessions.items():
        if not isinstance(record, dict):
            continue
        email = str(record.get("email") or key or "").strip().lower()
        password = str(record.get("password") or "").strip()
        if not email or not password or password.lower() == NULL_VALUE:
            continue
        save_compact_account(accounts_output, email, password, record)


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
    """写入账号紧凑格式：账号----密码----订阅类型----rt----at。"""

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
    """把旧账号行统一整理为账号----密码----订阅类型----rt----at。"""

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
        _field(_extract_access_token(token_data)),
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
        return [_field(parts[index]) for index in range(ACCOUNT_FIELD_COUNT)]
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


def _extract_refresh_token(token_data: dict[str, Any]) -> str:
    return str(token_data.get("refresh_token") or token_data.get("refreshToken") or "")


def _extract_access_token(token_data: dict[str, Any]) -> str:
    direct = str(token_data.get("access_token") or token_data.get("accessToken") or "").strip()
    if direct:
        return direct
    session_data = _chatgpt_session_data(token_data)
    if isinstance(session_data, dict):
        return str(session_data.get("accessToken") or session_data.get("access_token") or "")
    return ""


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
