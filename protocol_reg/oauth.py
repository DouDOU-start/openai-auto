from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any


AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
REDIRECT_URI = "http://localhost:1455/auth/callback"
SCOPE = "openid profile email offline_access"


@dataclass(frozen=True)
class OAuthStart:
    auth_url: str
    state: str
    verifier: str


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def start_oauth() -> OAuthStart:
    state = secrets.token_urlsafe(16)
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    return OAuthStart(f"{AUTH_URL}?{urllib.parse.urlencode(params)}", state, verifier)


def has_callback_code(url: str) -> bool:
    return "code=" in url and "state=" in url


def parse_callback(callback_url: str) -> dict[str, str]:
    candidate = callback_url.strip()
    if "://" not in candidate:
        candidate = f"http://localhost/?{candidate.lstrip('?')}"
    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)
    for key, values in fragment.items():
        if not query.get(key):
            query[key] = values
    return {key: (values[0] if values else "") for key, values in query.items()}


def jwt_claims_no_verify(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * ((4 - len(parts[1]) % 4) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload.encode("ascii")).decode("utf-8"))
    except Exception:
        return {}


def exchange_token(session: Any, callback_url: str, oauth: OAuthStart, proxies: Any, timeout: int) -> dict[str, Any]:
    callback = parse_callback(callback_url)
    if callback.get("state") != oauth.state:
        raise RuntimeError("OAuth state 不匹配")
    code = callback.get("code", "")
    if not code:
        raise RuntimeError("OAuth 回调缺少 code")

    resp = session.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": oauth.verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        proxies=proxies,
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OAuth 换 token 失败 HTTP {resp.status_code}: {resp.text[:500]}")

    data = resp.json()
    claims = jwt_claims_no_verify(data.get("id_token", ""))
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    plan_type = str(
        auth_claims.get("chatgpt_plan_type")
        or claims.get("chatgpt_plan_type")
        or ""
    ).strip()
    subscription_active_until = str(
        auth_claims.get("chatgpt_subscription_active_until")
        or claims.get("chatgpt_subscription_active_until")
        or ""
    ).strip()
    now = int(time.time())
    expires_in = int(data.get("expires_in") or 3600)
    result = {
        "id_token": data.get("id_token", ""),
        "client_id": CLIENT_ID,
        "access_token": data.get("access_token", ""),
        "refresh_token": data.get("refresh_token", ""),
        "account_id": auth_claims.get("chatgpt_account_id", ""),
        "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "email": claims.get("email", ""),
        "type": "codex",
        "expired": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + expires_in)),
    }
    if plan_type:
        result["plan_type"] = plan_type
    if subscription_active_until:
        result["subscription_active_until"] = subscription_active_until
    return result
