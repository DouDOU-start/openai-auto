from __future__ import annotations

import urllib.parse
from typing import Any

from curl_cffi import requests

from .auth_core_client import AuthCoreClient
from .settings import Settings
from .utils import absolutize_auth_url


DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36"
)


class OpenAIHTTP:
    def __init__(self, settings: Settings, auth_core: AuthCoreClient):
        self.settings = settings
        self.auth_core = auth_core
        self.session = requests.Session(proxies=settings.proxies, impersonate="chrome110")
        self.session.timeout = settings.timeout

    def headers(self, did: str, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "accept": "application/json",
            "accept-language": "en-US,en;q=0.9",
            "user-agent": DEFAULT_UA,
            "sec-ch-ua": '"Google Chrome";v="110", "Chromium";v="110", "Not_A Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "oai-device-id": did,
        }
        if extra:
            headers.update(extra)
        return headers

    def post_json(
        self,
        *,
        url: str,
        did: str,
        flow: str,
        proxy: str,
        user_agent: str,
        ctx: dict[str, Any],
        referer: str,
        payload: dict[str, Any],
    ) -> Any:
        sentinel = self.auth_core.payload(
            did=did,
            flow=flow,
            proxy=proxy,
            user_agent=user_agent,
            ctx=ctx,
        )
        headers = self.headers(did, {"Referer": referer, "content-type": "application/json"})
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        return self.session.post(
            url,
            headers=headers,
            json=payload,
            proxies=self.settings.proxies,
            verify=self.settings.ssl_verify,
            timeout=self.settings.timeout,
        )

    def follow_redirects(
        self,
        start_url: str,
        max_redirects: int = 12,
        headers: dict[str, str] | None = None,
    ) -> tuple[Any, str]:
        current = absolutize_auth_url(start_url)
        response = None
        request_headers = dict(headers or {})
        for _ in range(max_redirects):
            response = self.session.get(
                current,
                headers=request_headers or None,
                allow_redirects=False,
                proxies=self.settings.proxies,
                verify=self.settings.ssl_verify,
                timeout=self.settings.timeout,
            )
            if response.status_code not in (301, 302, 303, 307, 308):
                return response, current
            location = response.headers.get("Location", "")
            if not location:
                return response, current
            current = urllib.parse.urljoin(current, location)
            if "code=" in current and "state=" in current:
                return None, current
        return response, current

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
