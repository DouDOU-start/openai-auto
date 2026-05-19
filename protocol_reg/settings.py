from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


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

    @property
    def proxies(self) -> dict[str, str] | None:
        proxy = self.proxy.strip()
        if not proxy:
            return None
        if proxy.startswith("socks5://"):
            proxy = proxy.replace("socks5://", "socks5h://", 1)
        return {"http": proxy, "https": proxy}
