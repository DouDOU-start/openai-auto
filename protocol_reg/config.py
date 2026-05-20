from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class AppConfig:
    # 协议请求代理
    proxy: str = ""
    # 注册邮箱随机生成后缀
    email_suffixes: tuple[str, ...] = ()
    # Cloudflare-email 验证码 API
    email_code_api: str = ""
    email_code_key: str = ""
    email_code_sender_suffix: str = "openai.com"
    email_code_timeout: int = 120
    email_code_poll: float = 2.0


def load_app_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return AppConfig()

    proxy = str(raw.get("proxy") or "").strip()
    email_suffixes = _load_email_suffixes(raw.get("email_suffixes"))

    mail = raw.get("email_code")
    if not isinstance(mail, dict):
        mail = {}

    def _s(key: str, default: str = "") -> str:
        v = mail.get(key)
        return str(v).strip() if v is not None else default

    def _i(key: str, default: int) -> int:
        v = mail.get(key)
        try:
            return int(v)
        except Exception:
            return default

    def _f(key: str, default: float) -> float:
        v = mail.get(key)
        try:
            return float(v)
        except Exception:
            return default

    sender = _s("sender_suffix", "openai.com") or "openai.com"
    poll = _f("poll", 2.0)
    if poll < 0.5:
        poll = 0.5
    timeout = _i("timeout", 120)
    if timeout < 5:
        timeout = 5

    return AppConfig(
        proxy=proxy,
        email_suffixes=email_suffixes,
        email_code_api=_s("api", ""),
        email_code_key=_s("key", ""),
        email_code_sender_suffix=sender,
        email_code_timeout=timeout,
        email_code_poll=poll,
    )


def config_template() -> dict[str, Any]:
    return {
        "proxy": "",
        "email_suffixes": [],
        "email_code": {
            "api": "",
            "key": "",
            "sender_suffix": "openai.com",
            "timeout": 120,
            "poll": 2.0,
        }
    }


def _load_email_suffixes(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return ()

    suffixes: list[str] = []
    seen: set[str] = set()
    for item in items:
        suffix = str(item or "").strip().lower()
        if suffix.startswith("@"):
            suffix = suffix[1:]
        if not suffix or "@" in suffix or "." not in suffix:
            continue
        if suffix in seen:
            continue
        seen.add(suffix)
        suffixes.append(suffix)
    return tuple(suffixes)
