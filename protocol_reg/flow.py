from __future__ import annotations

import random
import re
import time
from typing import Any, Callable

from curl_cffi import requests

from .auth_core_client import AuthCoreClient
from .email_code_client import EmailCodeClient
from .oauth import exchange_token, has_callback_code, jwt_claims_no_verify, start_oauth
from .openai_http import DEFAULT_UA, OpenAIHTTP
from .settings import Settings
from .storage import apply_session_cookies, dump_session_cookies
from .utils import absolutize_auth_url, mask_email, random_profile


Prompt = Callable[[str], str]
PhoneSolver = Callable[..., str]
PHONE_CHANGE_COMMAND = "__protocol_reg_change_phone__"


class PhoneVerificationError(RuntimeError):
    """手机号验证失败，不应被授权流程当作登录会话失效处理。"""


_AUTH_RETRY_STATUS_CODES = {429, 500, 502, 503, 504}
_AUTH_RETRY_MAX_ATTEMPTS = 4
_AUTH_RETRY_BASE_DELAY_SECONDS = 30.0
_AUTH_RETRY_MAX_DELAY_SECONDS = 180.0


class RegisterFlow:
    def __init__(self, settings: Settings, prompt: Prompt, phone_solver: PhoneSolver | None = None):
        self.settings = settings
        self.prompt = prompt
        self.phone_solver = phone_solver
        self.auth_core = AuthCoreClient(settings.project_root, settings.license_file)
        self.http = OpenAIHTTP(settings, self.auth_core)
        self.email_code = EmailCodeClient(settings)
        self._last_token_cookies: list[dict[str, Any]] | None = None

    def run(self, email: str, password: str) -> dict[str, Any]:
        print(f"[注册] 初始化 auth_core 环境: {mask_email(email)}")
        did, user_agent = self.auth_core.init_auth(
            session=self.http.session,
            email=email,
            masked_email=mask_email(email),
            proxies=self.settings.proxies,
            verify=self.settings.ssl_verify,
        )
        if not did:
            raise RuntimeError("auth_core 未返回 oai-did")
        user_agent = user_agent or DEFAULT_UA
        ctx: dict[str, Any] = {"email": email}

        print("[注册] 提交邮箱")
        signup_resp = self._post_json_with_retry(
            self.http,
            label="提交邮箱",
            url="https://auth.openai.com/api/accounts/authorize/continue",
            did=did,
            flow="authorize_continue",
            proxy=self.settings.proxy,
            user_agent=user_agent,
            ctx=ctx,
            referer="https://auth.openai.com/create-account",
            payload={"username": {"value": email, "kind": "email"}, "screen_hint": "login_or_signup"},
        )
        self._ensure_status(signup_resp, 200, "提交邮箱")

        print("[注册] 设置密码")
        pwd_resp = self._post_json_with_retry(
            self.http,
            label="设置密码",
            url="https://auth.openai.com/api/accounts/user/register",
            did=did,
            flow="username_password_create",
            proxy=self.settings.proxy,
            user_agent=user_agent,
            ctx=ctx,
            referer="https://auth.openai.com/create-account/password",
            payload={"password": password, "username": email},
        )
        self._ensure_status(pwd_resp, 200, "设置密码")

        pwd_json = pwd_resp.json()
        target_url = self._next_url(pwd_json)
        if self._needs_otp(pwd_json, target_url):
            target_url = self._email_otp(email, did, user_agent, ctx)

        if self._is_phone_verification_url(target_url):
            target_url = self._handle_phone_verification(
                self.http,
                did=did,
                user_agent=user_agent,
                ctx=ctx,
                current_url=target_url,
                label="注册",
            )

        profile = random_profile()
        print(f"[注册] 创建账户资料: {profile['name']} / {profile['birthdate']}")
        create_resp = self.http.post_json(
            url="https://auth.openai.com/api/accounts/create_account",
            did=did,
            flow="create_account",
            proxy=self.settings.proxy,
            user_agent=user_agent,
            ctx=ctx,
            referer="https://auth.openai.com/about-you",
            payload=profile,
        )
        self._ensure_status(create_resp, 200, "创建账户资料")
        target_url = self._next_url(create_resp.json())
        if self._is_phone_verification_url(target_url):
            target_url = self._handle_phone_verification(
                self.http,
                did=did,
                user_agent=user_agent,
                ctx=ctx,
                current_url=target_url,
                label="注册",
            )

        if self.settings.login_delay > 0:
            print(f"[注册] 等待 {self.settings.login_delay} 秒后获取 ChatGPT session")
            time.sleep(self.settings.login_delay)

        return self._account_session_data(email, password, did, user_agent, target_url, "注册")

    def login(self, email: str, password: str, *, create_checkout: bool = True) -> dict[str, Any]:
        """仅登录已有账号并返回可持久化的登录会话。"""
        self._last_token_cookies = None
        self.http.session.cookies.clear()
        print(f"[登录] 初始化 auth_core 环境: {mask_email(email)}")
        did, user_agent = self.auth_core.init_auth(
            session=self.http.session,
            email=email,
            masked_email=mask_email(email),
            proxies=self.settings.proxies,
            verify=self.settings.ssl_verify,
        )
        if not did:
            raise RuntimeError("auth_core 未返回 oai-did")
        user_agent = user_agent or DEFAULT_UA
        ctx: dict[str, Any] = {"email": email}

        print("[登录] 打开登录链路")
        _, current = self.http.follow_redirects("https://auth.openai.com/log-in")
        if self._is_phone_verification_url(current):
            current = self._handle_phone_verification(
                self.http,
                did=did,
                user_agent=user_agent,
                ctx=ctx,
                current_url=current,
                label="登录",
            )

        current = self._password_login(email, password, did, user_agent, ctx, current, "登录")
        if self._is_phone_verification_url(current):
            current = self._handle_phone_verification(
                self.http,
                did=did,
                user_agent=user_agent,
                ctx=ctx,
                current_url=current,
                label="登录",
            )
        print("[登录] 登录完成，正在获取 ChatGPT session")
        chatgpt_session = self.fetch_chatgpt_session(self.http.session)
        session_data = self._session_snapshot(email, password, did, user_agent, current)
        session_data["chatgpt_session"] = chatgpt_session
        if create_checkout:
            session_data["plus_trial_checkout"] = self.create_plus_trial_checkout(
                self.http.session,
                session_data["chatgpt_session"],
            )
            session_data["cookies"] = dump_session_cookies(self.http.session)
        print("[登录] 已保存当前会话 cookies")
        return session_data

    def authorize_from_session(self, email: str, session_data: dict[str, Any]) -> dict[str, Any]:
        """仅使用已保存登录会话执行 OAuth 授权换 token。"""
        cookies = session_data.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            raise RuntimeError("登录会话缺少 cookies，请先执行 login 模式")
        apply_session_cookies(self.http.session, cookies)

        oauth = start_oauth()
        did = str(session_data.get("did") or self.http.session.cookies.get("oai-did") or "")
        user_agent = str(session_data.get("user_agent") or DEFAULT_UA)
        print(f"[授权] 使用已保存会话打开 OAuth 授权链路: {mask_email(email)}")
        _, current = self.http.follow_redirects(oauth.auth_url, headers=self._navigation_headers(did, user_agent))
        return self._complete_oauth(oauth, did, user_agent, current, self.http.session, email, "已保存会话")

    def authorize_current_session(self, email: str, session_data: dict[str, Any] | None = None) -> dict[str, Any]:
        """使用当前内存登录会话执行 OAuth 授权，避免重新套用可能过期的 cookie 快照。"""
        session_data = session_data or {}
        oauth = start_oauth()
        did = str(session_data.get("did") or self.http.session.cookies.get("oai-did") or "")
        user_agent = str(session_data.get("user_agent") or DEFAULT_UA)
        print(f"[授权] 使用当前登录会话打开 OAuth 授权链路: {mask_email(email)}")
        _, current = self.http.follow_redirects(oauth.auth_url, headers=self._navigation_headers(did, user_agent))
        return self._complete_oauth(oauth, did, user_agent, current, self.http.session, email, "当前登录会话")

    def authorize(self, email: str, password: str) -> dict[str, Any]:
        """兼容旧入口：登录和授权一次完成。CLI 默认不再使用该模式。"""
        login_data = self.login(email, password)
        return self.authorize_current_session(email, login_data)

    def fetch_chatgpt_session(self, session: requests.Session) -> dict[str, Any]:
        print("[身份] 获取 ChatGPT session 身份信息")
        try:
            try:
                session.get(
                    "https://chatgpt.com/",
                    headers={"accept": "text/html,*/*", "user-agent": DEFAULT_UA},
                    proxies=self.settings.proxies,
                    verify=self.settings.ssl_verify,
                    timeout=self.settings.timeout,
                )
            except Exception:
                pass
            resp = session.get(
                "https://chatgpt.com/api/auth/session",
                headers={
                    "accept": "application/json",
                    "referer": "https://chatgpt.com/",
                    "user-agent": DEFAULT_UA,
                },
                proxies=self.settings.proxies,
                verify=self.settings.ssl_verify,
                timeout=self.settings.timeout,
            )
        except Exception as exc:
            print(f"[警告] ChatGPT session 身份信息获取失败: {exc}")
            return {"status_code": 0, "error": str(exc)}
        result: dict[str, Any] = {"status_code": resp.status_code}
        try:
            data = resp.json()
            result["data"] = data
            account_type = self._extract_account_type(data)
            account_check = self.fetch_account_check(session, data) if not account_type else {}
            if account_check:
                result["account_check"] = account_check
                account_type = self._extract_account_type(account_check) or account_type
            if account_type:
                result["account_type"] = account_type
                result["subscription_type"] = account_type
        except Exception:
            result["text"] = resp.text[:1000]
        if resp.status_code != 200:
            print(f"[警告] ChatGPT session 身份信息返回 HTTP {resp.status_code}")
        return result

    def fetch_account_check(self, session: requests.Session, session_data: dict[str, Any]) -> dict[str, Any]:
        access_token = str(session_data.get("accessToken") or "").strip()
        if not access_token:
            return {}
        print("[身份] 获取账号类型")
        try:
            resp = session.get(
                "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                    "Referer": "https://chatgpt.com/",
                    "User-Agent": DEFAULT_UA,
                },
                proxies=self.settings.proxies,
                verify=self.settings.ssl_verify,
                timeout=self.settings.timeout,
            )
        except Exception as exc:
            print(f"[警告] 账号类型获取失败: {exc}")
            return {"status_code": 0, "error": str(exc)}

        result: dict[str, Any] = {"status_code": resp.status_code}
        try:
            result["data"] = resp.json()
        except Exception:
            result["text"] = resp.text[:500]
        if resp.status_code != 200:
            print(f"[警告] 账号类型接口返回 HTTP {resp.status_code}")
        return result

    def create_plus_trial_checkout(
        self,
        session: requests.Session,
        chatgpt_session: dict[str, Any],
    ) -> dict[str, Any]:
        print("[Plus] 正在获取 Session Token")
        session_data = chatgpt_session.get("data") if isinstance(chatgpt_session, dict) else None
        access_token = str((session_data or {}).get("accessToken") or "").strip() if isinstance(session_data, dict) else ""
        if not access_token:
            return {"status_code": 0, "error": "未获取到 access_token，请确认已登录 chatgpt.com"}
        print("[Plus] Token 获取成功")

        payload = {
            "plan_name": "chatgptplusplan",
            "billing_details": {"country": "US", "currency": "USD"},
            "cancel_url": "https://chatgpt.com/#pricing",
            "promo_campaign": {
                "promo_campaign_id": "plus-1-month-free",
                "is_coupon_from_query_param": True,
            },
            "checkout_ui_mode": "redirect",
        }
        checkout_user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        )
        print("[Plus] 正在请求 Stripe 长链接")
        try:
            resp = session.post(
                "https://chatgpt.com/backend-api/payments/checkout",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "Origin": "https://chatgpt.com",
                    "Referer": "https://chatgpt.com/",
                    "Accept-Language": "en-US,en;q=0.9",
                    "User-Agent": checkout_user_agent,
                },
                json=payload,
                impersonate="chrome136",
                proxies=self.settings.proxies,
                verify=self.settings.ssl_verify,
                timeout=self.settings.timeout,
            )
        except Exception as exc:
            print(f"[警告] Plus 网络请求异常: {exc}")
            return {"status_code": 0, "error": str(exc)}

        result: dict[str, Any] = {"status_code": resp.status_code, "payload": payload}
        try:
            data = resp.json()
            result["data"] = data
        except Exception:
            result["text"] = resp.text[:1000]
            data = {}

        if isinstance(data, dict):
            long_url = str(data.get("url") or data.get("stripe_hosted_url") or data.get("checkout_url") or "").strip()
            session_id = str(data.get("checkout_session_id") or "").strip()
            processor = str(data.get("processor_entity") or "openai_llc").strip() or "openai_llc"
            result["long_url"] = long_url
            result["hosted_url"] = long_url
            result["short_url"] = f"https://chatgpt.com/checkout/{processor}/{session_id}" if session_id else ""
            result["chatgpt_checkout_url"] = result["short_url"]
            if long_url.startswith("https://pay.openai.com/"):
                result["openai_payurl"] = long_url
        if resp.status_code >= 400:
            detail = data.get("detail") if isinstance(data, dict) else ""
            print(f"[警告] Plus 请求失败，HTTP {resp.status_code}: {detail or resp.text[:300]}")
        return result

    def _account_session_data(
        self,
        email: str,
        password: str,
        did: str,
        user_agent: str,
        target_url: str,
        label: str,
    ) -> dict[str, Any]:
        current = target_url
        if self._is_phone_verification_url(current):
            current = self._handle_phone_verification(
                self.http,
                did=did,
                user_agent=user_agent,
                ctx={"email": email},
                current_url=current,
                label=label,
            )
        elif current:
            _, current = self.http.follow_redirects(current)
        self._last_token_cookies = dump_session_cookies(self.http.session)
        chatgpt_session = self.fetch_chatgpt_session(self.http.session)
        data = {
            "type": "chatgpt_session",
            "email": email,
            "account_id": "",
            "current_url": current,
            "last_refresh": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "chatgpt_session": chatgpt_session,
        }
        session_payload = chatgpt_session.get("data")
        if isinstance(session_payload, dict):
            user = session_payload.get("user")
            if isinstance(user, dict):
                data["email"] = str(user.get("email") or email)
                data["account_id"] = str(user.get("id") or user.get("account_id") or "")
        print(f"[{label}] 已获取 ChatGPT session 身份信息")
        return data

    def close(self) -> None:
        self.http.close()

    def _post_json_with_retry(
        self,
        http: OpenAIHTTP,
        *,
        label: str,
        url: str,
        did: str,
        flow: str,
        proxy: str,
        user_agent: str,
        ctx: dict[str, Any],
        referer: str,
        payload: dict[str, Any],
    ) -> Any:
        last_error = ""
        for attempt in range(1, _AUTH_RETRY_MAX_ATTEMPTS + 1):
            try:
                resp = http.post_json(
                    url=url,
                    did=did,
                    flow=flow,
                    proxy=proxy,
                    user_agent=user_agent,
                    ctx=ctx,
                    referer=referer,
                    payload=payload,
                )
            except Exception as exc:
                last_error = str(exc)
                if attempt >= _AUTH_RETRY_MAX_ATTEMPTS:
                    raise
                delay = self._auth_retry_delay(attempt)
                print(f"[警告] {label} 请求异常，{delay:.0f}s 后重试: {exc}")
                time.sleep(delay)
                continue

            if resp.status_code in _AUTH_RETRY_STATUS_CODES and attempt < _AUTH_RETRY_MAX_ATTEMPTS:
                reason = self._response_text(resp)[:300] or f"HTTP {resp.status_code}"
                delay = self._auth_retry_delay(attempt)
                print(f"[警告] {label} 返回可重试错误，{delay:.0f}s 后重试: {reason}")
                time.sleep(delay)
                continue
            return resp

        raise RuntimeError(f"{label}请求失败: {last_error}")

    @staticmethod
    def _auth_retry_delay(attempt: int) -> float:
        attempt = max(1, int(attempt))
        delay = _AUTH_RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1))
        return min(_AUTH_RETRY_MAX_DELAY_SECONDS, delay)

    def _password_login(
        self,
        email: str,
        password: str,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
        current: str,
        label: str,
    ) -> str:
        print(f"[{label}] 提交账号邮箱")
        login_resp = self._post_json_with_retry(
            self.http,
            label=f"{label}邮箱提交",
            url="https://auth.openai.com/api/accounts/authorize/continue",
            did=did,
            flow="authorize_continue",
            proxy=self.settings.proxy,
            user_agent=user_agent,
            ctx=ctx,
            referer=current,
            payload={"username": {"value": email, "kind": "email"}},
        )
        self._ensure_status(login_resp, 200, f"{label}邮箱提交")

        next_page = self._next_url(login_resp.json())
        if next_page:
            _, current = self.http.follow_redirects(next_page)
            if self._is_phone_verification_url(current):
                current = self._handle_phone_verification(
                    self.http,
                    did=did,
                    user_agent=user_agent,
                    ctx=ctx,
                    current_url=current,
                    label=label,
                )

        print(f"[{label}] 校验账号密码")
        pwd_resp = self._post_json_with_retry(
            self.http,
            label=f"{label}密码校验",
            url="https://auth.openai.com/api/accounts/password/verify",
            did=did,
            flow="password_verify",
            proxy=self.settings.proxy,
            user_agent=user_agent,
            ctx=ctx,
            referer=current,
            payload={"password": password},
        )
        self._ensure_status(pwd_resp, 200, f"{label}密码校验")

        next_page = self._next_url(pwd_resp.json())
        if not next_page:
            raise RuntimeError(f"{label}密码校验后未返回下一步地址")
        _, current = self.http.follow_redirects(next_page)

        if current.endswith("/email-verification"):
            current = self._authorize_email_otp(did, user_agent, ctx)
        return current

    def _email_otp(self, email: str, did: str, user_agent: str, ctx: dict[str, Any]) -> str:
        return self._solve_email_otp(
            http=self.http,
            label="注册",
            email=email,
            did=did,
            user_agent=user_agent,
            ctx=ctx,
            send_referer="https://auth.openai.com/create-account/password",
            prompt_text="请输入邮箱收到的 6 位验证码；未收到可直接回车触发重发: ",
        )

    def _authorize_email_otp(self, did: str, user_agent: str, ctx: dict[str, Any]) -> str:
        next_page = self._solve_email_otp(
            http=self.http,
            label="登录",
            email=str(ctx.get("email") or ""),
            did=did,
            user_agent=user_agent,
            ctx=ctx,
            send_referer="https://auth.openai.com/email-verification",
            prompt_text="请输入登录邮箱验证码；未收到可直接回车触发重发: ",
        )
        _, current = self.http.follow_redirects(next_page)
        return current

    def _extract_token(
        self,
        oauth: Any,
        target_url: str,
        email: str,
        password: str,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
    ) -> dict[str, Any]:
        for candidate in [target_url, oauth.auth_url]:
            if not candidate:
                continue
            _, current = self.http.follow_redirects(candidate)
            if has_callback_code(current):
                return self._exchange_and_attach_session(self.http.session, current, oauth)
            if self._is_phone_verification_url(current):
                current = self._handle_phone_verification(
                    self.http,
                    did=did,
                    user_agent=user_agent,
                    ctx=ctx,
                    current_url=current,
                    label="授权",
                )
                if has_callback_code(current):
                    return self._exchange_and_attach_session(self.http.session, current, oauth)
                continue
            if "/workspace" in current or current.endswith("/consent"):
                selected = self._select_workspace(did, current)
                _, final = self.http.follow_redirects(selected)
                if has_callback_code(final):
                    return self._exchange_and_attach_session(self.http.session, final, oauth)

        print("[登录] 未直接捕获 OAuth 回调，开始补一次静默登录链路")
        return self._login_and_extract(email, password, did, user_agent, ctx)

    def _login_and_extract(
        self,
        email: str,
        password: str,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
    ) -> dict[str, Any]:
        login_oauth = start_oauth()
        login_http = OpenAIHTTP(self.settings, self.auth_core)
        try:
            _, current = login_http.follow_redirects(login_oauth.auth_url)
            if self._is_phone_verification_url(current):
                current = self._handle_phone_verification(
                    login_http,
                    did=did,
                    user_agent=user_agent,
                    ctx=ctx,
                    current_url=current,
                    label="授权",
                )
            login_resp = self._post_json_with_retry(
                login_http,
                label="登录邮箱提交",
                url="https://auth.openai.com/api/accounts/authorize/continue",
                did=did,
                flow="authorize_continue",
                proxy=self.settings.proxy,
                user_agent=user_agent,
                ctx=ctx,
                referer=current,
                payload={"username": {"value": email, "kind": "email"}},
            )
            self._ensure_status(login_resp, 200, "登录第一步")
            next_page = self._next_url(login_resp.json())
            if not next_page:
                raise RuntimeError("登录第一步未返回下一步地址")
            _, current = login_http.follow_redirects(next_page)
            if self._is_phone_verification_url(current):
                current = self._handle_phone_verification(
                    login_http,
                    did=did,
                    user_agent=user_agent,
                    ctx=ctx,
                    current_url=current,
                    label="授权",
                )
            pwd_resp = self._post_json_with_retry(
                login_http,
                label="密码登录",
                url="https://auth.openai.com/api/accounts/password/verify",
                did=did,
                flow="password_verify",
                proxy=self.settings.proxy,
                user_agent=user_agent,
                ctx=ctx,
                referer=current,
                payload={"password": password},
            )
            self._ensure_status(pwd_resp, 200, "密码登录")
            next_page = self._next_url(pwd_resp.json())
            if not next_page:
                raise RuntimeError("密码登录后未返回下一步地址")
            _, current = login_http.follow_redirects(next_page)
            if self._is_phone_verification_url(current):
                current = self._handle_phone_verification(
                    login_http,
                    did=did,
                    user_agent=user_agent,
                    ctx=ctx,
                    current_url=current,
                    label="授权",
                )
            if current.endswith("/email-verification"):
                next_page = self._solve_email_otp(
                    http=login_http,
                    label="登录",
                    email=email,
                    did=did,
                    user_agent=user_agent,
                    ctx=ctx,
                    send_referer="https://auth.openai.com/email-verification",
                    prompt_text="登录二次验证：请输入邮箱验证码；未收到可直接回车触发重发: ",
                )
                _, current = login_http.follow_redirects(next_page)
                if self._is_phone_verification_url(current):
                    current = self._handle_phone_verification(
                        login_http,
                        did=did,
                        user_agent=user_agent,
                        ctx=ctx,
                        current_url=current,
                        label="授权",
                    )
            if "/workspace" in current or current.endswith("/consent"):
                selected = self._select_workspace_with_session(login_http.session, did, current)
                _, current = login_http.follow_redirects(selected)
            if not has_callback_code(current):
                raise RuntimeError(f"未捕获 OAuth 回调，当前地址: {current}")
            return self._exchange_and_attach_session(login_http.session, current, login_oauth)
        finally:
            login_http.close()

    def _complete_oauth(
        self,
        oauth: Any,
        did: str,
        user_agent: str,
        current: str,
        session: requests.Session,
        email: str,
        session_label: str,
    ) -> dict[str, Any]:
        for _ in range(6):
            if has_callback_code(current):
                return self._exchange_and_attach_session(session, current, oauth)
            if self._is_phone_verification_url(current):
                current = self._handle_phone_verification(
                    self.http,
                    did=did,
                    user_agent=user_agent or DEFAULT_UA,
                    ctx={"email": email},
                    current_url=current,
                    label="授权",
                )
                continue
            if self._is_saved_account_selection_url(current):
                selected = self._try_select_saved_account(session, did, current, email)
                if not selected:
                    break
                if has_callback_code(selected):
                    current = selected
                    continue
                _, current = self.http.follow_redirects(selected, headers=self._navigation_headers(did, user_agent))
                continue
            if "/workspace" in current or current.endswith("/consent"):
                selected = self._select_workspace_with_session(session, did, current)
                if selected == current:
                    break
                if has_callback_code(selected):
                    current = selected
                    continue
                _, current = self.http.follow_redirects(selected, headers=self._navigation_headers(did, user_agent))
                continue
            break
        print(f"[诊断] {session_label} cookie 摘要: {self._cookie_debug_summary(session)}")
        raise RuntimeError(f"{session_label}未完成 OAuth 授权，当前地址: {current}")

    def _select_saved_account(
        self,
        session: requests.Session,
        did: str,
        current_url: str,
        email: str,
    ) -> str:
        resp = session.get(
            current_url,
            headers=self._navigation_headers(did, referer=current_url),
            allow_redirects=False,
            proxies=self.settings.proxies,
            verify=self.settings.ssl_verify,
            timeout=self.settings.timeout,
        )
        location = str(getattr(resp, "headers", {}).get("Location", "") or "").strip()
        if location:
            return absolutize_auth_url(location)
        response_url = str(getattr(resp, "url", "") or "")
        if has_callback_code(response_url):
            return response_url
        self._ensure_status(resp, 200, "读取账号选择页")
        sessions = self._parse_unified_sessions(resp.text)
        target = self._pick_unified_session_id(sessions, email)
        if not target:
            raise RuntimeError("账号选择页没有找到可用登录会话")
        print(f"[授权] 自动选择账号: {mask_email(email)}")
        select_resp = session.post(
            "https://auth.openai.com/api/accounts/session/select",
            headers=self.http.headers(did, {"Referer": current_url, "content-type": "application/json"}),
            json={"session_id": target},
            proxies=self.settings.proxies,
            verify=self.settings.ssl_verify,
            timeout=self.settings.timeout,
        )
        self._ensure_status(select_resp, 200, "选择登录账号")
        return self._next_url(select_resp.json()) or current_url

    def _try_select_saved_account(
        self,
        session: requests.Session,
        did: str,
        current_url: str,
        email: str,
    ) -> str:
        try:
            return self._select_saved_account(session, did, current_url, email)
        except RuntimeError as exc:
            if "没有找到可用登录会话" not in str(exc):
                raise
            print(f"[授权] 当前页面没有可自动选择的登录会话: {exc}")
            return ""

    def _exchange_and_attach_session(
        self,
        session: requests.Session,
        current: str,
        oauth: Any,
    ) -> dict[str, Any]:
        token_data = exchange_token(session, current, oauth, self.settings.proxies, self.settings.timeout)
        self._last_token_cookies = dump_session_cookies(session)
        token_data["chatgpt_session"] = self.fetch_chatgpt_session(session)
        return token_data

    def _session_snapshot(
        self,
        email: str,
        password: str,
        did: str,
        user_agent: str,
        current_url: str,
    ) -> dict[str, Any]:
        return {
            "email": email,
            "password": password,
            "did": did,
            "user_agent": user_agent,
            "current_url": current_url,
            "cookies": dump_session_cookies(self.http.session),
        }

    def _select_workspace(self, did: str, current_url: str) -> str:
        return self._select_workspace_with_session(self.http.session, did, current_url)

    def _select_workspace_with_session(self, session: requests.Session, did: str, current_url: str) -> str:
        workspaces = self._parse_workspaces(session.cookies.get("oai-client-auth-session") or "")
        if not workspaces:
            return current_url
        workspace_id = str((workspaces[0] or {}).get("id") or "")
        if not workspace_id:
            return current_url
        resp = session.post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers=self.http.headers(did, {"Referer": current_url, "content-type": "application/json"}),
            json={"workspace_id": workspace_id},
            proxies=self.settings.proxies,
            verify=self.settings.ssl_verify,
            timeout=self.settings.timeout,
        )
        if resp.status_code != 200:
            return current_url
        return self._next_url(resp.json()) or current_url

    @staticmethod
    def _is_saved_account_selection_url(url: str) -> bool:
        text = str(url or "").lower()
        return "/choose-an-account" in text or "/log-in" in text or "/oauth/authorize" in text

    def _navigation_headers(
        self,
        did: str,
        user_agent: str = "",
        *,
        referer: str = "",
    ) -> dict[str, str]:
        extra = {
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "user-agent": user_agent or DEFAULT_UA,
            "upgrade-insecure-requests": "1",
            "sec-fetch-dest": "document",
            "sec-fetch-mode": "navigate",
            "sec-fetch-site": "same-origin" if referer else "none",
            "sec-fetch-user": "?1",
        }
        if referer:
            extra["Referer"] = referer
        return self.http.headers(did, extra)

    @staticmethod
    def _cookie_debug_summary(session: requests.Session) -> str:
        items: list[str] = []
        try:
            jar = session.cookies.jar
        except Exception:
            return "无法读取 cookie"
        for cookie in jar:
            domain = str(getattr(cookie, "domain", "") or "")
            if "openai" not in domain and "chatgpt" not in domain:
                continue
            name = str(getattr(cookie, "name", "") or "")
            if not name:
                continue
            path = str(getattr(cookie, "path", "") or "/")
            items.append(f"{domain}{path}:{name}")
        return ", ".join(sorted(set(items))) or "空"

    def _handle_phone_verification(
        self,
        http: OpenAIHTTP,
        *,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
        current_url: str,
        label: str,
    ) -> str:
        if self.phone_solver is not None:
            return self.phone_solver(
                flow=self,
                http=http,
                did=did,
                user_agent=user_agent,
                ctx=ctx,
                current_url=current_url,
                label=label,
            )
        return self._manual_phone_verification(
            flow=self,
            http=http,
            did=did,
            user_agent=user_agent,
            ctx=ctx,
            current_url=current_url,
            label=label,
        )

    def _manual_phone_verification(
        self,
        *,
        flow: "RegisterFlow" | None = None,
        http: OpenAIHTTP,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
        current_url: str,
        label: str,
    ) -> str:
        _ = flow
        print(f"[{label}] 触发手机号验证，需要人工输入手机号和短信验证码")
        current = absolutize_auth_url(current_url or "https://auth.openai.com/add-phone")
        max_retries = max(1, int(self.settings.otp_max_retries or 1))
        last_error = ""
        phone_attempt = 1
        while True:
            phone = self._read_phone_number(label, phone_attempt, max_retries, last_error)
            send_resp = self._send_phone_number(http, did, user_agent, ctx, current, phone)
            if send_resp.status_code == 200:
                current = self._next_url_from_response(send_resp) or "https://auth.openai.com/phone-verification"
                code_attempt = 1
                while True:
                    code = self.prompt(
                        f"请输入手机号 {phone} 收到的短信验证码（第 {code_attempt}/{max_retries} 次，留空重发，输入“更换手机号”可换号）: "
                    ).strip()
                    if self._is_change_phone_command(code):
                        print(f"[{label}] 已请求更换手机号")
                        last_error = "已请求更换手机号"
                        current = "https://auth.openai.com/add-phone"
                        phone_attempt += 1
                        break
                    if not code:
                        last_error = "验证码为空"
                        self._resend_phone_otp(http, did, user_agent, ctx, current, label)
                        continue
                    verify_resp = self._validate_phone_otp(http, did, user_agent, ctx, current, code)
                    if verify_resp.status_code == 200:
                        next_url = self._next_url_from_response(verify_resp) or current
                        _, final_url = http.follow_redirects(next_url)
                        print(f"[{label}] 手机号验证完成")
                        return final_url
                    last_error = self._response_text(verify_resp) or verify_resp.text[:300] or f"HTTP {verify_resp.status_code}"
                    if self._should_retry_phone_otp(verify_resp, last_error):
                        print(f"[警告] {label} 手机验证码错误，继续等待重新输入: {last_error}")
                        code_attempt += 1
                        if code_attempt > max_retries:
                            print(f"[警告] {label} 手机验证码已连续失败 {max_retries} 次，继续等待验证码；也可以点击更换手机号")
                            code_attempt = 1
                        continue
                    print(f"[警告] {label} 手机验证码校验未通过，继续等待重新输入或更换手机号: {last_error}")
                    code_attempt += 1
                    if code_attempt > max_retries:
                        code_attempt = 1
                    continue
                continue
            last_error = self._phone_submit_error(send_resp)
            if self._should_retry_phone_number(send_resp, last_error):
                print(f"[警告] {label} 手机号不可用，请更换手机号后重试: {last_error}")
                phone_attempt += 1
                continue
            raise PhoneVerificationError(f"{label}手机号提交失败 HTTP {send_resp.status_code}: {last_error}")

    @staticmethod
    def _is_change_phone_command(value: str) -> bool:
        text = str(value or "").strip().lower()
        return text in {PHONE_CHANGE_COMMAND, "change-phone", "change_phone", "更换手机号", "换手机号"}

    def _read_phone_number(self, label: str, attempt: int = 1, max_retries: int = 1, last_error: str = "") -> str:
        while True:
            prompt = f"[{label}] 请输入手机号（必须带国家码，例如 +8613800000000，第 {attempt} 次）: "
            if last_error == "已请求更换手机号":
                prompt = f"[{label}] 请重新输入手机号（必须带国家码，例如 +8613800000000，第 {attempt} 次）: "
            elif last_error:
                prompt = f"[{label}] 上一个手机号不可用：{last_error}。请更换手机号（第 {attempt} 次）: "
            phone = self.prompt(prompt).strip()
            if phone:
                return phone
            print(f"[警告] {label} 手机号不能为空")

    def _send_phone_number(
        self,
        http: OpenAIHTTP,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
        referer: str,
        phone: str,
    ) -> Any:
        print("[手机] 提交手机号并发送短信验证码")
        resp = self._post_auth_json(
            http,
            url="https://auth.openai.com/api/accounts/add-phone/send",
            did=did,
            flow="authorize_continue",
            user_agent=user_agent,
            ctx=ctx,
            referer=referer or "https://auth.openai.com/add-phone",
            payload={"phone_number": phone},
        )
        print(f"[手机] add-phone/send -> HTTP {resp.status_code}")
        if resp.status_code == 200:
            print("[手机] 短信验证码发送请求已成功提交")
        return resp

    def _phone_submit_error(self, resp: Any) -> str:
        return self._response_text(resp) or str(getattr(resp, "text", "") or "")[:500] or f"HTTP {resp.status_code}"

    @staticmethod
    def _should_retry_phone_number(resp: Any, error_text: str) -> bool:
        text = str(error_text or "").lower()
        if str(getattr(resp, "status_code", "") or "") == "400":
            return True
        return any(marker in text for marker in ("fraud_guard", "invalid_request_error", "phone", "number"))

    @staticmethod
    def _should_retry_phone_otp(resp: Any, error_text: str) -> bool:
        status = int(getattr(resp, "status_code", 0) or 0)
        text = str(error_text or "").lower()
        if status in {401, 429, 500, 502, 503, 504}:
            return True
        if status == 400 and any(
            marker in text
            for marker in (
                "invalid otp",
                "invalid_otp",
                "invalid code",
                "invalid_code",
                "invalid_input",
                "please try again",
                "otp",
            )
        ):
            return True
        return False

    def _resend_phone_otp(
        self,
        http: OpenAIHTTP,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
        referer: str,
        label: str,
    ) -> None:
        print(f"[{label}] 重发手机短信验证码")
        resp = self._post_auth_json(
            http,
            url="https://auth.openai.com/api/accounts/phone-otp/resend",
            did=did,
            flow="authorize_continue",
            user_agent=user_agent,
            ctx=ctx,
            referer=referer or "https://auth.openai.com/phone-verification",
            payload={},
        )
        print(f"[{label}] phone-otp/resend -> HTTP {resp.status_code}")
        if resp.status_code != 200:
            print(f"[警告] {label} 手机验证码重发失败 HTTP {resp.status_code}: {self._response_text(resp) or resp.text[:300]}")
        else:
            print(f"[{label}] 手机短信验证码重发请求已成功提交")

    def _validate_phone_otp(
        self,
        http: OpenAIHTTP,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
        referer: str,
        code: str,
    ) -> Any:
        resp = self._post_auth_json(
            http,
            url="https://auth.openai.com/api/accounts/phone-otp/validate",
            did=did,
            flow="authorize_continue",
            user_agent=user_agent,
            ctx=ctx,
            referer=referer or "https://auth.openai.com/phone-verification",
            payload={"code": code},
        )
        print(f"[手机] phone-otp/validate -> HTTP {resp.status_code}")
        return resp

    def _post_auth_json(
        self,
        http: OpenAIHTTP,
        *,
        url: str,
        did: str,
        flow: str,
        user_agent: str,
        ctx: dict[str, Any],
        referer: str,
        payload: dict[str, Any],
    ) -> Any:
        sentinel = self.auth_core.payload(
            did=did,
            flow=flow,
            proxy=self.settings.proxy,
            user_agent=user_agent,
            ctx=ctx,
        )
        headers = http.headers(
            did,
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Origin": "https://auth.openai.com",
                "Referer": referer,
                "user-agent": user_agent or DEFAULT_UA,
                **self._datadog_trace_headers(),
            },
        )
        if sentinel:
            headers["openai-sentinel-token"] = sentinel
        return http.session.post(
            url,
            headers=headers,
            json=payload,
            allow_redirects=False,
            proxies=self.settings.proxies,
            verify=self.settings.ssl_verify,
            timeout=self.settings.timeout,
        )

    @staticmethod
    def _next_url_from_response(resp: Any) -> str:
        try:
            data = resp.json()
        except Exception:
            data = {}
        if isinstance(data, dict):
            next_url = RegisterFlow._next_url(data)
            if next_url:
                return next_url
        location = str(getattr(resp, "headers", {}).get("Location", "") or "").strip()
        return absolutize_auth_url(location) if location else ""

    @staticmethod
    def _is_phone_verification_url(url: str) -> bool:
        text = str(url or "").lower()
        return "/add-phone" in text or "/phone-verification" in text or "phone-otp" in text

    @staticmethod
    def _datadog_trace_headers() -> dict[str, str]:
        trace_id = str(random.getrandbits(64))
        parent_id = str(random.getrandbits(64))
        trace_hex = f"{int(trace_id):016x}"
        parent_hex = f"{int(parent_id):016x}"
        return {
            "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
            "tracestate": "dd=s:1;o:rum",
            "x-datadog-origin": "rum",
            "x-datadog-parent-id": parent_id,
            "x-datadog-sampling-priority": "1",
            "x-datadog-trace-id": trace_id,
        }

    @staticmethod
    def _parse_workspaces(cookie_value: str) -> list:
        if not cookie_value:
            return []
        for segment in cookie_value.split("."):
            claims = jwt_claims_no_verify(f"x.{segment}.x")
            workspaces = claims.get("workspaces") or []
            if isinstance(workspaces, list) and workspaces:
                return workspaces
        return []

    @staticmethod
    def _parse_unified_sessions(page_text: str) -> list[dict[str, str]]:
        normalized = page_text.replace('\\"', '"')
        sessions: list[dict[str, str]] = []
        seen: set[str] = set()
        for match in re.finditer(r"us_[A-Za-z0-9]{10,}", normalized):
            session_id = match.group(0)
            if session_id in seen:
                continue
            seen.add(session_id)
            if session_id == "us_capture":
                continue
            window = normalized[match.start() : match.start() + 2500]
            email_match = re.search(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}", window)
            sessions.append({"id": session_id, "email": email_match.group(0).lower() if email_match else ""})
        return sessions

    @staticmethod
    def _pick_unified_session_id(sessions: list[dict[str, str]], email: str) -> str:
        expected = email.strip().lower()
        for item in sessions:
            if item.get("email") == expected:
                return str(item.get("id") or "")
        if sessions:
            return str(sessions[0].get("id") or "")
        return ""

    @staticmethod
    def _extract_account_type(payload: object) -> str:
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
        if isinstance(payload, dict):
            for key in key_names:
                value = payload.get(key)
                if isinstance(value, (str, int, float)) and str(value).strip():
                    return str(value).strip()
                if isinstance(value, dict):
                    nested = RegisterFlow._extract_account_type(value)
                    if nested:
                        return nested
            if isinstance(payload.get("is_paid_subscription_active"), bool):
                return "paid" if payload["is_paid_subscription_active"] else "free"
            for value in payload.values():
                nested = RegisterFlow._extract_account_type(value)
                if nested:
                    return nested
        elif isinstance(payload, list):
            for item in payload:
                nested = RegisterFlow._extract_account_type(item)
                if nested:
                    return nested
        return ""

    @staticmethod
    def _next_url(data: dict[str, Any]) -> str:
        cont = str(data.get("continue_url") or "").strip()
        if cont:
            return absolutize_auth_url(cont)
        page_type = str((data.get("page") or {}).get("type") or "").strip()
        mapping = {
            "email_otp_verification": "https://auth.openai.com/email-verification",
            "sign_in_with_chatgpt_codex_consent": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
            "workspace": "https://auth.openai.com/workspace",
            "add_phone": "https://auth.openai.com/add-phone",
            "phone_verification": "https://auth.openai.com/phone-verification",
            "phone_otp_verification": "https://auth.openai.com/phone-verification",
            "phone_number_verification": "https://auth.openai.com/phone-verification",
        }
        return mapping.get(page_type, "")

    @staticmethod
    def _needs_otp(data: dict[str, Any], url: str) -> bool:
        page_type = str((data.get("page") or {}).get("type") or "")
        return "otp" in page_type or "verify" in url or "email-verification" in url

    @staticmethod
    def _ensure_status(resp: Any, expected: int, label: str) -> None:
        if resp.status_code == expected:
            return
        if resp.status_code == 403:
            raise RuntimeError(f"{label}触发 403，当前授权/代理/风控环境不可用")
        raise RuntimeError(f"{label}失败 HTTP {resp.status_code}: {resp.text[:500]}")

    def _solve_email_otp(
        self,
        http: OpenAIHTTP,
        *,
        email: str,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
        label: str,
        send_referer: str,
        prompt_text: str,
    ) -> str:
        max_retries = max(1, int(self.settings.otp_max_retries or 1))
        recipient = str(email or ctx.get("email") or "").strip()
        last_error = ""
        for attempt in range(1, max_retries + 1):
            print(f"[{label}] 触发邮箱验证码发送（第 {attempt}/{max_retries} 轮）")
            try:
                send_resp = http.post_json(
                    url="https://auth.openai.com/api/accounts/email-otp/send",
                    did=did,
                    flow="authorize_continue",
                    proxy=self.settings.proxy,
                    user_agent=user_agent,
                    ctx=ctx,
                    referer=send_referer,
                    payload={},
                )
            except Exception as exc:
                last_error = f"发信异常: {exc}"
                if attempt < max_retries:
                    print(f"[警告] {label} 发信异常，准备重试: {exc}")
                    continue
                break
            if send_resp.status_code != 200:
                print(f"[警告] {label} 发信接口返回 HTTP {send_resp.status_code}: {send_resp.text[:300]}")

            try:
                code = self._read_email_otp_code(label, recipient, prompt_text)
            except Exception as exc:
                last_error = f"读取验证码失败: {exc}"
                if attempt < max_retries:
                    print(f"[警告] {label} 读取验证码失败，准备重试: {exc}")
                    continue
                break

            if not code:
                last_error = "验证码不能为空"
                if attempt < max_retries:
                    print(f"[警告] {label} 验证码为空，准备重试")
                    continue
                break

            try:
                otp_resp = http.post_json(
                    url="https://auth.openai.com/api/accounts/email-otp/validate",
                    did=did,
                    flow="authorize_continue",
                    proxy=self.settings.proxy,
                    user_agent=user_agent,
                    ctx=ctx,
                    referer="https://auth.openai.com/email-verification",
                    payload={"code": code},
                )
            except Exception as exc:
                last_error = f"验证码校验异常: {exc}"
                if attempt < max_retries:
                    print(f"[警告] {label} 验证码校验异常，准备重试: {exc}")
                    continue
                break

            retry_reason = self._retryable_email_otp_error(otp_resp)
            if retry_reason:
                last_error = retry_reason
                if attempt < max_retries:
                    print(f"[警告] {label} 验证码校验失败，准备重试: {retry_reason}")
                    continue
                break

            self._ensure_status(otp_resp, 200, f"{label}验证码校验")
            next_page = self._next_url(otp_resp.json())
            if next_page:
                return next_page

            last_error = "验证码校验后未返回下一步地址"
            if attempt < max_retries:
                print(f"[警告] {label} 验证码校验后未返回下一步地址，准备重试")
                continue
            break

        raise RuntimeError(f"{label}邮箱验证码校验失败，已重试 {max_retries} 轮: {last_error}")

    def _read_email_otp_code(self, label: str, recipient: str, prompt_text: str) -> str:
        if self.email_code.enabled() and recipient:
            print(f"[{label}] 等待邮箱验证码（cloudflare-email）")
            result = self.email_code.wait_code(recipient, max_attempts=self.settings.otp_poll_max_attempts)
            code = result.code.strip()
            print(f"[{label}] 已获取验证码: {code}")
            return code
        return self.prompt(prompt_text).strip()

    @staticmethod
    def _retryable_email_otp_error(resp: Any) -> str:
        text = RegisterFlow._response_text(resp)
        lowered = text.lower()
        if "wrong_email_otp_code" in lowered:
            return "wrong_email_otp_code"
        if "otp" in lowered and any(token in lowered for token in ("wrong", "invalid", "expired")):
            return text[:300] or "邮箱验证码校验失败"
        if resp.status_code in {429, 500, 502, 503, 504}:
            return text[:300] or f"HTTP {resp.status_code}"
        return ""

    @staticmethod
    def _response_text(resp: Any) -> str:
        try:
            data = resp.json()
        except Exception:
            return str(getattr(resp, "text", "") or "").strip()
        parts: list[str] = []
        RegisterFlow._collect_text(data, parts)
        text = " ".join(part for part in parts if part).strip()
        return text or str(getattr(resp, "text", "") or "").strip()

    @staticmethod
    def _collect_text(value: Any, parts: list[str]) -> None:
        if value is None:
            return
        if isinstance(value, (str, int, float, bool)):
            text = str(value).strip()
            if text:
                parts.append(text)
            return
        if isinstance(value, dict):
            for item in value.values():
                RegisterFlow._collect_text(item, parts)
            return
        if isinstance(value, list):
            for item in value:
                RegisterFlow._collect_text(item, parts)
