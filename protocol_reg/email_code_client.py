from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from curl_cffi import requests

from .settings import Settings


@dataclass(frozen=True)
class EmailCodeResult:
    code: str
    message_id: int | None = None
    raw: dict[str, Any] | None = None


class EmailCodeClient:
    """Client for DouDOU-start/cloudflare-email email-code API.

    Docs: docs/email-code-api.md
    POST /api/code/email
    Authorization: Bearer <ADMIN_API_KEY>
    """

    def __init__(self, settings: Settings):
        self._settings = settings

    @staticmethod
    def _norm_base(base: str) -> str:
        base = (base or "").strip()
        return base[:-1] if base.endswith("/") else base

    def enabled(self) -> bool:
        return bool(self._settings.email_code_api_base.strip() and self._settings.email_code_api_key.strip())

    def fetch_code_once(self, recipient: str, *, mark_read: bool = True) -> EmailCodeResult | None:
        base = self._norm_base(self._settings.email_code_api_base)
        if not base:
            return None
        url = f"{base}/api/code/email"
        headers = {
            "Authorization": f"Bearer {self._settings.email_code_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {
            "platform": "openai",
            "recipient": recipient,
            "sender_suffix": self._settings.email_code_sender_suffix or "openai.com",
            "mark_read": bool(mark_read),
        }

        # 404 means: no unread matching emails. That isn't an error for polling.
        resp = requests.post(
            url,
            headers=headers,
            json=payload,
            proxies=self._settings.proxies,
            verify=self._settings.ssl_verify,
            timeout=self._settings.timeout,
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"email-code API 返回 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        code = str(data.get("code") or "").strip()
        if not code:
            raise RuntimeError(f"email-code API 响应缺少 code 字段: {data}")
        mid = data.get("message_id")
        message_id = int(mid) if isinstance(mid, int) or (isinstance(mid, str) and mid.isdigit()) else None
        return EmailCodeResult(code=code, message_id=message_id, raw=data if isinstance(data, dict) else None)

    def wait_code(self, recipient: str) -> EmailCodeResult:
        deadline = time.time() + float(self._settings.email_code_timeout)
        interval = float(self._settings.email_code_poll_interval)
        last_err: Exception | None = None
        while time.time() < deadline:
            try:
                result = self.fetch_code_once(recipient, mark_read=True)
                if result is not None:
                    return result
            except Exception as exc:
                # Treat transient errors as retryable within timeout.
                last_err = exc
            time.sleep(interval)

        suffix = self._settings.email_code_sender_suffix or "openai.com"
        msg = f"等待邮箱验证码超时（{self._settings.email_code_timeout}s）：{recipient} sender_suffix={suffix}"
        if last_err is not None:
            msg += f"，最后一次错误: {last_err}"
        raise RuntimeError(msg)

