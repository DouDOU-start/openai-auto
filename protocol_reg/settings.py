from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


_PROXY_SPLIT_RE = re.compile(r"[\s,]+")


def parse_proxy_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_items = _split_proxy_text(value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = []
        for item in value:
            raw_items.extend(_split_proxy_text(str(item or "")))
    else:
        raw_items = _split_proxy_text(str(value or ""))

    proxies: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        proxy = item.strip()
        if not proxy or proxy in seen:
            continue
        seen.add(proxy)
        proxies.append(proxy)
    return tuple(proxies)


def resolve_proxy_pool(*candidates: object) -> tuple[str, ...]:
    for candidate in candidates:
        proxies = parse_proxy_list(candidate)
        if proxies:
            return proxies
    return ()


def proxy_to_requests_proxies(proxy: str) -> dict[str, str] | None:
    proxy = str(proxy or "").strip()
    if not proxy:
        return None
    if proxy.startswith("socks5://"):
        proxy = proxy.replace("socks5://", "socks5h://", 1)
    return {"http": proxy, "https": proxy}


def proxy_preview(proxy: str) -> str:
    proxy = str(proxy or "").strip()
    if not proxy:
        return "无代理"
    scheme, sep, rest = proxy.partition("://")
    if sep and "@" in rest:
        return f"{scheme}://***@{rest.rsplit('@', 1)[1]}"
    if "@" in proxy:
        return f"***@{proxy.rsplit('@', 1)[1]}"
    return proxy


def _split_proxy_text(value: str) -> list[str]:
    text = value.strip()
    if not text:
        return []
    return [item for item in _PROXY_SPLIT_RE.split(text) if item.strip()]


@dataclass(frozen=True)
class Settings:
    project_root: Path
    proxy: str
    output: Path
    session_file: Path
    license_file: Path | None
    login_delay: int
    timeout: int
    ssl_verify: bool

    # cloudflare-email 验证码 API
    email_code_api_base: str
    email_code_api_key: str
    email_code_sender_suffix: str
    email_code_poll_interval: float
    email_code_timeout: int
    otp_max_retries: int = 5
    otp_poll_max_attempts: int = 20
    use_proxy_for_email: bool = False

    @property
    def email_code_proxies(self) -> dict[str, str] | None:
        return self.proxies if self.use_proxy_for_email else None

    @property
    def proxies(self) -> dict[str, str] | None:
        return proxy_to_requests_proxies(self.proxy)
