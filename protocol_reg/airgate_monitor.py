from __future__ import annotations

from dataclasses import dataclass
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import time
from typing import Any, Callable

from curl_cffi import requests

from .flow import RegisterFlow
from .settings import Settings
from .storage import (
    ACCOUNT_STATUS_ABANDONED,
    NULL_VALUE,
    get_account_db_by_email,
    save_login_session_db,
    update_account_status_db,
)


PromptFactory = Callable[[str], str]
SettingsFactory = Callable[[], Settings]


@dataclass(frozen=True)
class AirGateMonitorConfig:
    enabled: bool = False
    core_url: str = ""
    admin_key: str = ""
    proxy: str = ""
    poll_interval_seconds: int = 300
    page_size: int = 100
    relogin_concurrency: int = 3


class AirGateAdminError(RuntimeError):
    pass


class AirGateCoreClient:
    def __init__(self, base_url: str, admin_key: str, *, timeout: int = 30):
        base = str(base_url or "").strip().rstrip("/")
        if base.endswith("/api/v1"):
            self.base_url = base
        else:
            self.base_url = f"{base}/api/v1"
        self.admin_key = str(admin_key or "").strip()
        self.timeout = max(1, int(timeout or 30))

    def list_accounts(self, *, platform: str = "openai", state: str = "", page_size: int = 100) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        page = 1
        total = None
        while True:
            params: dict[str, Any] = {
                "platform": platform,
                "page": page,
                "page_size": min(100, max(1, int(page_size or 100))),
            }
            if state:
                params["state"] = state
            payload = self._request(
                "GET",
                "/admin/accounts",
                params=params,
            )
            batch = payload.get("list") if isinstance(payload, dict) else None
            if not isinstance(batch, list):
                batch = []
            for item in batch:
                if isinstance(item, dict):
                    items.append(item)
            if isinstance(payload, dict) and total is None:
                try:
                    total = int(payload.get("total") or 0)
                except Exception:
                    total = 0
            if not batch:
                break
            if total is not None and len(items) >= total:
                break
            page += 1
            if page > 20:
                break
        return items

    def update_account(self, account_id: int, *, credentials: dict[str, str], state: str = "active") -> dict[str, Any]:
        payload = {
            "credentials": credentials,
            "state": state,
        }
        data = self._request("PUT", f"/admin/accounts/{int(account_id)}", json_body=payload)
        return data if isinstance(data, dict) else {}

    def delete_account(self, account_id: int) -> None:
        self._request("DELETE", f"/admin/accounts/{int(account_id)}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        if not self.admin_key:
            raise AirGateAdminError("未配置 AirGate admin key")
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.admin_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=self.timeout,
                impersonate="chrome136",
            )
        except Exception as exc:
            raise AirGateAdminError(f"AirGate 请求失败: {exc}") from exc

        text = resp.text or ""
        try:
            data = resp.json()
        except Exception:
            data = None

        if resp.status_code >= 400:
            message = _response_message(data, text)
            raise AirGateAdminError(f"AirGate HTTP {resp.status_code}: {message}")
        if isinstance(data, dict) and int(data.get("code") or 0) != 0:
            raise AirGateAdminError(_response_message(data, text))
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data


class AirGate401Monitor:
    def __init__(
        self,
        config: AirGateMonitorConfig,
        settings_factory: SettingsFactory,
        *,
        prompt_factory: PromptFactory | None = None,
    ):
        self._config = config
        self._settings_factory = settings_factory
        self._prompt_factory = prompt_factory or _fail_prompt
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = False
        self._run_count = 0
        self._last_run_at: float | None = None
        self._next_run_at: float | None = None
        self._last_error = ""
        self._last_success: list[str] = []
        self._last_skipped: list[str] = []
        self._last_failed: list[str] = []
        self._last_deleted: list[str] = []
        self._last_candidates = 0

    def start(self, config: AirGateMonitorConfig | None = None) -> dict[str, Any]:
        with self._lock:
            if config is not None:
                self._config = _merge_config(self._config, config)
            self._enabled = True
            self._last_error = ""
            self._next_run_at = time.time()
            if self._thread is None or not self._thread.is_alive():
                self._wake.clear()
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
            else:
                self._wake.set()
            return self.status()

    def configure(self, config: AirGateMonitorConfig) -> dict[str, Any]:
        with self._lock:
            self._config = _merge_config(self._config, config)
            if self._enabled:
                self._next_run_at = time.time()
                self._wake.set()
            return self.status()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._enabled = False
            self._next_run_at = None
            self._wake.set()
            return self.status()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "configured": bool(self._config.core_url.strip() and self._config.admin_key.strip()),
                "core_url": self._config.core_url,
                "proxy": self._config.proxy,
                "admin_key_configured": bool(self._config.admin_key.strip()),
                "poll_interval_seconds": self._config.poll_interval_seconds,
                "page_size": self._config.page_size,
                "relogin_concurrency": self._config.relogin_concurrency,
                "run_count": self._run_count,
                "last_run_at": self._last_run_at,
                "next_run_at": self._next_run_at,
                "last_error": self._last_error,
                "last_success": list(self._last_success[-20:]),
                "last_skipped": list(self._last_skipped[-20:]),
                "last_failed": list(self._last_failed[-20:]),
                "last_deleted": list(self._last_deleted[-20:]),
                "last_candidates": self._last_candidates,
            }

    def run_once(self) -> dict[str, Any]:
        return self._run_once()

    def _loop(self) -> None:
        while True:
            with self._lock:
                if not self._enabled:
                    return
                next_run_at = self._next_run_at or time.time()
                delay = max(0.0, next_run_at - time.time())
            if self._wake.wait(delay):
                self._wake.clear()
                continue
            self._run_once()
            with self._lock:
                if not self._enabled:
                    return
                self._next_run_at = time.time() + max(10, self._config.poll_interval_seconds)

    def _run_once(self) -> dict[str, Any]:
        config = self._snapshot_config()
        if not config.core_url.strip() or not config.admin_key.strip():
            with self._lock:
                self._last_error = "AirGate 监控未配置 core_url 或 admin_key"
                self._last_run_at = time.time()
            return self.status()

        client = AirGateCoreClient(config.core_url, config.admin_key, timeout=self._settings_factory().timeout)
        try:
            candidates = client.list_accounts(platform="openai", page_size=config.page_size)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._last_run_at = time.time()
                self._last_candidates = 0
            return self.status()

        success: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        deleted: list[str] = []
        processed = 0
        jobs: list[tuple[dict[str, Any], str]] = []
        for account in candidates:
            if not isinstance(account, dict):
                continue
            email = _account_email(account)
            if not email:
                if _looks_like_401_account(account):
                    processed += 1
                    skipped.append(_describe_account(account, "missing email"))
                continue
            local_account = get_account_db_by_email(email)
            if _is_local_account_abandoned(local_account):
                try:
                    client.delete_account(int(account.get("id") or 0))
                except Exception as exc:
                    failed.append(f"{_describe_account(account, 'delete failed')}: {exc}")
                    print(f"[AirGate] 删除已废弃账号失败: {email} (id={account.get('id')}) -> {exc}")
                else:
                    deleted.append(_describe_account(account, "deleted local abandoned"))
                    print(f"[AirGate] 已删除 core 中的本地废弃账号: {email} (id={account.get('id')})")
                continue
            if not _looks_like_401_account(account):
                continue
            processed += 1
            if local_account is None:
                skipped.append(_describe_account(account, "local missing"))
                print(f"[AirGate] 跳过本地账号池缺失的账号: {email} (id={account.get('id')})")
                continue
            print(f"[AirGate] 准备修复 core 账号: {email} (id={account.get('id')})")
            jobs.append((account, email))

        if jobs:
            workers = min(max(1, int(config.relogin_concurrency or 1)), len(jobs))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="airgate-relogin") as pool:
                future_map = {
                    pool.submit(self._relogin_and_update, account, email, config): (account, email)
                    for account, email in jobs
                }
                for future in as_completed(future_map):
                    account, email = future_map[future]
                    try:
                        result = future.result()
                    except Exception as exc:
                        failed.append(f"{_describe_account(account, 'failed')}: {exc}")
                        print(f"[AirGate] 修复失败: {email} (id={account.get('id')}) -> {exc}")
                        continue
                    if result:
                        success.append(result)
                        print(f"[AirGate] 修复完成: {result}")

        with self._lock:
            self._run_count += 1
            self._last_run_at = time.time()
            self._next_run_at = self._last_run_at + max(10, config.poll_interval_seconds)
            self._last_error = ""
            self._last_success = success
            self._last_skipped = skipped
            self._last_failed = failed
            self._last_deleted = deleted
            self._last_candidates = processed
        return self.status()

    def _relogin_and_update(self, core_account: dict[str, Any], email: str, config: AirGateMonitorConfig) -> str:
        local_account = get_account_db_by_email(email)
        if local_account is None:
            raise AirGateAdminError("本地账号池里没有这个邮箱")
        password = str(local_account.get("password") or "").strip()
        if not password or password.lower() == NULL_VALUE:
            raise AirGateAdminError("本地账号缺少密码，无法重新登录")

        settings = self._settings_factory()
        settings = _override_proxy(settings, config)
        flow = RegisterFlow(settings, prompt=self._prompt_factory)
        try:
            print(f"[AirGate] 重新登录获取 session: {email}")
            try:
                session_data = flow.login(email, password, create_checkout=False)
            except Exception as exc:
                if _is_forbidden_error(exc):
                    self._abandon_local_account(local_account)
                    raise AirGateAdminError(f"登录触发 403，已将本地账号废弃: {email}") from exc
                raise
        finally:
            try:
                flow.close()
            except Exception:
                pass

        session_payload = _session_payload(session_data)
        access_token = _session_access_token(session_payload)
        if not access_token:
            raise AirGateAdminError("登录完成但未拿到新的 session accessToken")

        saved = save_login_session_db(email, password, session_data) or local_account
        merged_credentials = _build_credentials(core_account, saved, session_payload, session_data)
        client = AirGateCoreClient(config.core_url, config.admin_key, timeout=settings.timeout)
        client.update_account(int(core_account.get("id") or 0), credentials=merged_credentials, state="active")
        return f"{email} -> core#{core_account.get('id')}"

    def _abandon_local_account(self, local_account: dict[str, Any]) -> None:
        account_id = int(local_account.get("id") or 0)
        if account_id <= 0:
            return
        current_status = str(local_account.get("status") or "").strip()
        if current_status == ACCOUNT_STATUS_ABANDONED:
            return
        try:
            update_account_status_db(account_id, ACCOUNT_STATUS_ABANDONED)
            print(f"[AirGate] 已将本地账号废弃: {local_account.get('email')} (id={account_id})")
        except Exception as exc:
            print(f"[AirGate] 废弃本地账号失败: {local_account.get('email')} (id={account_id}) -> {exc}")

    def _snapshot_config(self) -> AirGateMonitorConfig:
        with self._lock:
            return self._config

    def current_config(self) -> AirGateMonitorConfig:
        return self._snapshot_config()


def _build_credentials(
    core_account: dict[str, Any],
    local_account: dict[str, Any],
    session_payload: dict[str, Any],
    session_data: dict[str, Any],
) -> dict[str, str]:
    existing = core_account.get("credentials") if isinstance(core_account.get("credentials"), dict) else {}
    result: dict[str, str] = {}
    for key, value in existing.items():
        text = _clean_text(value)
        if text:
            result[str(key)] = text

    access_token = _session_access_token(session_payload)
    if not access_token:
        raise AirGateAdminError("session 中缺少 accessToken")
    result["access_token"] = access_token
    session_token = _session_nested_text(session_payload, "sessionToken") or _session_nested_text(session_payload, "session_token")
    if session_token:
        result["session_token"] = session_token
    account_id = _session_nested_text(session_payload, "account", "id")
    if account_id:
        result["chatgpt_account_id"] = account_id
    email = _session_nested_text(session_payload, "user", "email") or _clean_text(local_account.get("email"))
    if email:
        result["email"] = email
    plan_type = _session_nested_text(session_payload, "account", "planType") or _clean_text(local_account.get("subscription_type"))
    if plan_type:
        result["plan_type"] = plan_type
    refresh_token = _clean_text(local_account.get("refresh_token"))
    if refresh_token and refresh_token.lower() != NULL_VALUE:
        result["refresh_token"] = refresh_token
    if "base_url" not in result:
        result["base_url"] = ""
    if "provider" not in result:
        result["provider"] = ""
    if "subscription_active_until" not in result:
        result["subscription_active_until"] = _session_nested_text(session_data, "subscription_active_until") or ""
    return result


def _override_proxy(settings: Settings, config: AirGateMonitorConfig) -> Settings:
    proxy = str(config.proxy or "").strip() or settings.proxy
    return Settings(
        project_root=settings.project_root,
        proxy=proxy,
        license_file=settings.license_file,
        login_delay=settings.login_delay,
        timeout=settings.timeout,
        ssl_verify=settings.ssl_verify,
        email_code_api_base=settings.email_code_api_base,
        email_code_api_key=settings.email_code_api_key,
        email_code_sender_suffix=settings.email_code_sender_suffix,
        email_code_poll_interval=settings.email_code_poll_interval,
        email_code_timeout=settings.email_code_timeout,
        otp_max_retries=settings.otp_max_retries,
        otp_poll_max_attempts=settings.otp_poll_max_attempts,
        use_proxy_for_email=settings.use_proxy_for_email,
        smsbower_api_base=settings.smsbower_api_base,
        smsbower_api_key=settings.smsbower_api_key,
        smsbower_service=settings.smsbower_service,
        smsbower_country=settings.smsbower_country,
        smsbower_max_price=settings.smsbower_max_price,
        smsbower_min_price=settings.smsbower_min_price,
        smsbower_provider_ids=settings.smsbower_provider_ids,
        smsbower_except_provider_ids=settings.smsbower_except_provider_ids,
        smsbower_phone_exception=settings.smsbower_phone_exception,
        smsbower_timeout=settings.smsbower_timeout,
        smsbower_poll_interval=settings.smsbower_poll_interval,
        use_proxy_for_smsbower=settings.use_proxy_for_smsbower,
        smsbower_reuse_limit=settings.smsbower_reuse_limit,
    )


def _session_payload(session_data: dict[str, Any]) -> dict[str, Any]:
    chatgpt_session = session_data.get("chatgpt_session")
    if not isinstance(chatgpt_session, dict):
        return {}
    data = chatgpt_session.get("data")
    if isinstance(data, dict):
        return data
    if "accessToken" in chatgpt_session or "user" in chatgpt_session:
        return chatgpt_session
    return {}


def _session_access_token(session_payload: dict[str, Any]) -> str:
    return _session_nested_text(session_payload, "accessToken") or _session_nested_text(session_payload, "access_token")


def _session_nested_text(value: object, *path: str) -> str:
    current: object = value
    for key in path:
        if not isinstance(current, dict):
            return ""
        current = current.get(key)
    return _clean_text(current)


def _clean_text(value: object) -> str:
    text = str(value or "").strip()
    if not text or text.lower() == NULL_VALUE:
        return ""
    return text


def _looks_like_401_account(account: dict[str, Any]) -> bool:
    if _clean_text(account.get("state")).lower() != "disabled":
        return False
    if str(account.get("platform") or "").strip().lower() != "openai":
        return False
    account_type = str(account.get("type") or "").strip().lower()
    if account_type in {"apikey", "api_key"}:
        return False
    reason = " ".join(
        [
            _clean_text(account.get("error_msg")),
            _clean_text(_credential_value(account, "error_msg")),
        ]
    ).lower()
    return (
        "401" in reason
        or "unauthorized" in reason
        or "authentication token" in reason
        or "token invalid" in reason
        or "invalidated" in reason
        or "expired" in reason
        or "失效" in reason
        or "无效" in reason
    )


def _credential_value(account: dict[str, Any], key: str) -> str:
    credentials = account.get("credentials")
    if not isinstance(credentials, dict):
        return ""
    return _clean_text(credentials.get(key))


def _account_email(account: dict[str, Any]) -> str:
    credentials = account.get("credentials")
    if isinstance(credentials, dict):
        for key in ("email", "account_email", "username"):
            text = _clean_text(credentials.get(key))
            if text:
                return text
    name = _clean_text(account.get("name"))
    if "@" in name:
        return name
    return ""


def _describe_account(account: dict[str, Any], suffix: str) -> str:
    email = _account_email(account) or _clean_text(account.get("name")) or f"#{account.get('id')}"
    return f"{email} ({suffix})"


def _is_local_account_abandoned(account: dict[str, Any] | None) -> bool:
    if account is None:
        return False
    return str(account.get("status") or "").strip() == ACCOUNT_STATUS_ABANDONED


def _response_message(data: object, fallback_text: str) -> str:
    if isinstance(data, dict):
        for key in ("message", "error", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("message") or value.get("error")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    text = fallback_text.strip()
    return text[:500] if text else "unknown error"


def _mask_url(url: str) -> str:
    text = str(url or "").strip().rstrip("/")
    if not text:
        return ""
    if "://" in text:
        scheme, rest = text.split("://", 1)
        host = rest.split("/", 1)[0]
        return f"{scheme}://{host}"
    return text


def _fail_prompt(prompt: str) -> str:
    raise RuntimeError(f"监控任务不支持交互输入: {prompt}")


def _is_forbidden_error(exc: Exception) -> bool:
    return "403" in str(exc or "")


def _merge_config(current: AirGateMonitorConfig, incoming: AirGateMonitorConfig) -> AirGateMonitorConfig:
    return AirGateMonitorConfig(
        enabled=bool(incoming.enabled or current.enabled),
        core_url=_prefer_text(incoming.core_url, current.core_url),
        admin_key=_prefer_text(incoming.admin_key, current.admin_key),
        proxy=_prefer_text(incoming.proxy, current.proxy),
        poll_interval_seconds=_prefer_int(incoming.poll_interval_seconds, current.poll_interval_seconds, minimum=10),
        page_size=min(100, max(1, _prefer_int(incoming.page_size, current.page_size, minimum=1))),
        relogin_concurrency=min(10, max(1, _prefer_int(incoming.relogin_concurrency, current.relogin_concurrency, minimum=1))),
    )


def _prefer_text(value: str, fallback: str) -> str:
    text = str(value or "").strip()
    return text if text else str(fallback or "").strip()


def _prefer_int(value: int, fallback: int, *, minimum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = 0
    if parsed < minimum:
        try:
            fallback_value = int(fallback)
        except Exception:
            fallback_value = minimum
        return max(minimum, fallback_value)
    return parsed
