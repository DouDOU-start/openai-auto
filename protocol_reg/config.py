from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .settings import parse_proxy_list


@dataclass(frozen=True)
class AppConfig:
    # 协议请求代理
    proxy: str = ""
    # 多代理列表；出网调用会按列表顺序轮询使用
    proxies: tuple[str, ...] = ()
    # Web 端任务最大并发数
    max_concurrency: int = 3
    # 注册邮箱随机生成后缀
    email_suffixes: tuple[str, ...] = ()
    # Cloudflare-email 验证码 API
    email_code_api: str = ""
    email_code_key: str = ""
    email_code_sender_suffix: str = "openai.com"
    email_code_timeout: int = 120
    email_code_poll: float = 2.0
    # 邮箱验证码失败后的重试轮次
    otp_max_retries: int = 5
    # 每轮等待验证码时的最大轮询次数
    otp_poll_max_attempts: int = 20
    # 邮箱验证码 API 是否也走代理
    use_proxy_for_email: bool = False


def load_app_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return AppConfig()

    proxy = str(raw.get("proxy") or "").strip()
    proxies = parse_proxy_list(raw.get("proxies")) or parse_proxy_list(proxy)
    max_concurrency = _positive_int(raw.get("max_concurrency"), 3)
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
    otp_max_retries = _positive_int(mail.get("max_otp_retries"), 5)
    otp_poll_max_attempts = _positive_int(mail.get("otp_poll_max_attempts"), 20)
    use_proxy_value = mail.get("use_proxy")
    if use_proxy_value in {None, ""} and "use_proxy_for_email" in mail:
        use_proxy_value = mail.get("use_proxy_for_email")
    use_proxy_for_email = _bool(use_proxy_value, False)

    return AppConfig(
        proxy=proxy,
        proxies=proxies,
        max_concurrency=max_concurrency,
        email_suffixes=email_suffixes,
        email_code_api=_s("api", ""),
        email_code_key=_s("key", ""),
        email_code_sender_suffix=sender,
        email_code_timeout=timeout,
        email_code_poll=poll,
        otp_max_retries=otp_max_retries,
        otp_poll_max_attempts=otp_poll_max_attempts,
        use_proxy_for_email=use_proxy_for_email,
    )


def config_template() -> dict[str, Any]:
    return {
        "proxy": "",
        "proxies": [],
        "max_concurrency": 3,
        "email_suffixes": [],
        "email_code": {
            "api": "",
            "key": "",
            "sender_suffix": "openai.com",
            "timeout": 120,
            "poll": 2.0,
            "max_otp_retries": 5,
            "otp_poll_max_attempts": 20,
            "use_proxy": False,
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


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default
