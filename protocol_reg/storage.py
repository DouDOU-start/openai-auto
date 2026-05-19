from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as handle:
        handle.write(f"{email}----{password}\n")


def save_credentials_rt_txt(output: Path, email: str, password: str, refresh_token: str) -> None:
    """Append a compact line format: email----password----rt.

    This is intentionally a plain text file for easy copy/paste into other tools.
    """

    output.parent.mkdir(parents=True, exist_ok=True)
    rt = (refresh_token or "").strip()
    with output.open("a", encoding="utf-8") as handle:
        handle.write(f"{email}----{password}----{rt}\n")


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
