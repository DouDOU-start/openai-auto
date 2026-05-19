from __future__ import annotations

import re
import time
from typing import Any, Callable

from curl_cffi import requests

from .auth_core_client import AuthCoreClient
from .oauth import exchange_token, has_callback_code, jwt_claims_no_verify, start_oauth
from .openai_http import DEFAULT_UA, OpenAIHTTP
from .settings import Settings
from .storage import apply_session_cookies, dump_session_cookies
from .utils import absolutize_auth_url, mask_email, random_profile


Prompt = Callable[[str], str]


class RegisterFlow:
    def __init__(self, settings: Settings, prompt: Prompt):
        self.settings = settings
        self.prompt = prompt
        self.auth_core = AuthCoreClient(settings.project_root, settings.license_file)
        self.http = OpenAIHTTP(settings, self.auth_core)
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
        ctx: dict[str, Any] = {}

        print("[注册] 提交邮箱")
        signup_resp = self.http.post_json(
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
        pwd_resp = self.http.post_json(
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
            target_url = self._email_otp(did, user_agent, ctx)

        if "/add-phone" in target_url:
            raise RuntimeError("注册触发手机号验证，第一阶段未实现接码")

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
        if "/add-phone" in target_url:
            print("[警告] 创建账户后返回手机号验证页，跳过 OAuth 授权并直接尝试获取 ChatGPT session")

        if self.settings.login_delay > 0:
            print(f"[注册] 等待 {self.settings.login_delay} 秒后获取 ChatGPT session")
            time.sleep(self.settings.login_delay)

        session_data = self._account_session_data(email, password, did, user_agent, target_url, "注册")
        session_data["plus_trial_checkout"] = self.create_plus_trial_checkout(
            self.http.session,
            session_data["chatgpt_session"],
        )
        return session_data

    def login(self, email: str, password: str) -> dict[str, Any]:
        """仅登录已有账号并返回可持久化的登录会话。"""
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
        ctx: dict[str, Any] = {}

        print("[登录] 打开登录链路")
        _, current = self.http.follow_redirects("https://auth.openai.com/log-in")
        if "/add-phone" in current:
            raise RuntimeError("登录链路触发手机号验证，第一阶段未实现接码")

        current = self._password_login(email, password, did, user_agent, ctx, current, "登录")
        if "/add-phone" in current:
            raise RuntimeError("登录链路触发手机号验证，第一阶段未实现接码")
        print("[登录] 登录完成，已保存当前会话 cookies")
        session_data = self._session_snapshot(email, password, did, user_agent, current)
        session_data["chatgpt_session"] = self.fetch_chatgpt_session(self.http.session)
        session_data["plus_trial_checkout"] = self.create_plus_trial_checkout(
            self.http.session,
            session_data["chatgpt_session"],
        )
        return session_data

    def authorize_from_session(self, email: str, session_data: dict[str, Any]) -> dict[str, Any]:
        """仅使用已保存登录会话执行 OAuth 授权换 token。"""
        cookies = session_data.get("cookies")
        if not isinstance(cookies, list) or not cookies:
            raise RuntimeError("登录会话缺少 cookies，请先执行 login 模式")
        apply_session_cookies(self.http.session, cookies)

        oauth = start_oauth()
        did = str(session_data.get("did") or self.http.session.cookies.get("oai-did") or "")
        print(f"[授权] 使用已保存会话打开 OAuth 授权链路: {mask_email(email)}")
        _, current = self.http.follow_redirects(oauth.auth_url)
        return self._complete_oauth(oauth, did, current, self.http.session, email)

    def authorize(self, email: str, password: str) -> dict[str, Any]:
        """兼容旧入口：登录和授权一次完成。CLI 默认不再使用该模式。"""
        login_data = self.login(email, password)
        return self.authorize_from_session(email, login_data)

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
            result["data"] = resp.json()
        except Exception:
            result["text"] = resp.text[:1000]
        if resp.status_code != 200:
            print(f"[警告] ChatGPT session 身份信息返回 HTTP {resp.status_code}")
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
            "checkout_ui_mode": "hosted",
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
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
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
        if current and "/add-phone" not in current:
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
        login_resp = self.http.post_json(
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

        print(f"[{label}] 校验账号密码")
        pwd_resp = self.http.post_json(
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

    def _email_otp(self, did: str, user_agent: str, ctx: dict[str, Any]) -> str:
        print("[注册] 触发邮箱验证码发送")
        send_resp = self.http.post_json(
            url="https://auth.openai.com/api/accounts/email-otp/send",
            did=did,
            flow="authorize_continue",
            proxy=self.settings.proxy,
            user_agent=user_agent,
            ctx=ctx,
            referer="https://auth.openai.com/create-account/password",
            payload={},
        )
        if send_resp.status_code != 200:
            print(f"[警告] 发信接口返回 HTTP {send_resp.status_code}: {send_resp.text[:300]}")
        code = self.prompt("请输入邮箱收到的 6 位验证码: ").strip()
        if not code:
            raise RuntimeError("验证码不能为空")
        otp_resp = self.http.post_json(
            url="https://auth.openai.com/api/accounts/email-otp/validate",
            did=did,
            flow="authorize_continue",
            proxy=self.settings.proxy,
            user_agent=user_agent,
            ctx=ctx,
            referer="https://auth.openai.com/email-verification",
            payload={"code": code},
        )
        self._ensure_status(otp_resp, 200, "验证码校验")
        return self._next_url(otp_resp.json())

    def _authorize_email_otp(self, did: str, user_agent: str, ctx: dict[str, Any]) -> str:
        code = self.prompt("请输入登录邮箱验证码；未收到可直接回车触发重发: ").strip()
        if not code:
            send_resp = self.http.post_json(
                url="https://auth.openai.com/api/accounts/email-otp/send",
                did=did,
                flow="authorize_continue",
                proxy=self.settings.proxy,
                user_agent=user_agent,
                ctx=ctx,
                referer="https://auth.openai.com/email-verification",
                payload={},
            )
            if send_resp.status_code != 200:
                print(f"[警告] 登录验证码发信返回 HTTP {send_resp.status_code}: {send_resp.text[:300]}")
            code = self.prompt("请输入登录邮箱验证码: ").strip()
        if not code:
            raise RuntimeError("登录邮箱验证码不能为空")
        otp_resp = self.http.post_json(
            url="https://auth.openai.com/api/accounts/email-otp/validate",
            did=did,
            flow="authorize_continue",
            proxy=self.settings.proxy,
            user_agent=user_agent,
            ctx=ctx,
            referer="https://auth.openai.com/email-verification",
            payload={"code": code},
        )
        self._ensure_status(otp_resp, 200, "登录邮箱验证码校验")
        next_page = self._next_url(otp_resp.json())
        if not next_page:
            raise RuntimeError("登录邮箱验证码校验后未返回下一步地址")
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
            if "/add-phone" in current:
                raise RuntimeError("OAuth 链路触发手机号验证，第一阶段未实现接码")
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
            login_resp = login_http.post_json(
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
            pwd_resp = login_http.post_json(
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
            if current.endswith("/email-verification"):
                code = self.prompt("登录二次验证：请输入邮箱验证码: ").strip()
                if not code:
                    raise RuntimeError("登录二次验证码不能为空")
                otp_resp = login_http.post_json(
                    url="https://auth.openai.com/api/accounts/email-otp/validate",
                    did=did,
                    flow="authorize_continue",
                    proxy=self.settings.proxy,
                    user_agent=user_agent,
                    ctx=ctx,
                    referer="https://auth.openai.com/email-verification",
                    payload={"code": code},
                )
                self._ensure_status(otp_resp, 200, "登录二次验证码")
                next_page = self._next_url(otp_resp.json())
                if not next_page:
                    raise RuntimeError("登录二次验证码校验后未返回下一步地址")
                _, current = login_http.follow_redirects(next_page)
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
        current: str,
        session: requests.Session,
        email: str,
    ) -> dict[str, Any]:
        for _ in range(6):
            if has_callback_code(current):
                return self._exchange_and_attach_session(session, current, oauth)
            if "/add-phone" in current:
                raise RuntimeError("OAuth 链路触发手机号验证，第一阶段未实现接码")
            if "/choose-an-account" in current:
                selected = self._select_saved_account(session, did, current, email)
                _, current = self.http.follow_redirects(selected)
                continue
            if "/workspace" in current or current.endswith("/consent"):
                selected = self._select_workspace_with_session(session, did, current)
                if selected == current:
                    break
                _, current = self.http.follow_redirects(selected)
                continue
            break
        raise RuntimeError(f"已保存会话未完成 OAuth 授权，当前地址: {current}")

    def _select_saved_account(
        self,
        session: requests.Session,
        did: str,
        current_url: str,
        email: str,
    ) -> str:
        resp = session.get(
            current_url,
            headers=self.http.headers(did, {"Referer": current_url}),
            proxies=self.settings.proxies,
            verify=self.settings.ssl_verify,
            timeout=self.settings.timeout,
        )
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
            "cookies": self._last_token_cookies or dump_session_cookies(self.http.session),
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
            "phone_verification": "https://auth.openai.com/add-phone",
            "phone_otp_verification": "https://auth.openai.com/add-phone",
            "phone_number_verification": "https://auth.openai.com/add-phone",
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
