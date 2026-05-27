from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Any, Callable

from .flow import PhoneVerificationError, RegisterFlow
from .openai_http import OpenAIHTTP
from .settings import Settings
from .smsbower_client import SmsBowerActivation, SmsBowerApiError, SmsBowerClient, SmsBowerNoNumber, SmsBowerTimeout
from .utils import absolutize_auth_url


SmsBowerLog = Callable[[str], None]


@dataclass
class _PooledActivation:
    activation_id: str
    phone_number: str
    successful_uses: int = 0
    received_codes: int = 0


class SmsBowerPhonePool:
    """Serializes Web phone verification so one rented number can be reused safely."""

    def __init__(self, settings: Settings, *, log: SmsBowerLog | None = None):
        self._settings = settings
        self._client = SmsBowerClient(settings)
        self._reuse_limit = max(1, int(settings.smsbower_reuse_limit or 3))
        self._log = log or (lambda _message: None)
        self._condition = threading.Condition(threading.RLock())
        self._active: _PooledActivation | None = None
        self._busy = False

    def enabled(self) -> bool:
        return self._client.enabled()

    def solve(
        self,
        *,
        flow: RegisterFlow,
        http: OpenAIHTTP,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
        current_url: str,
        label: str,
        log: SmsBowerLog | None = None,
    ) -> str:
        if not self.enabled():
            raise PhoneVerificationError("SMSBower 未配置 api_key")
        active_log = log or self._log
        with self._lease_slot(active_log):
            return self._solve_locked(
                flow=flow,
                http=http,
                did=did,
                user_agent=user_agent,
                ctx=ctx,
                current_url=current_url,
                label=label,
                log=active_log,
            )

    def _solve_locked(
        self,
        *,
        flow: RegisterFlow,
        http: OpenAIHTTP,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
        current_url: str,
        label: str,
        log: SmsBowerLog,
    ) -> str:
        current = absolutize_auth_url(current_url or "https://auth.openai.com/add-phone")
        max_phone_attempts = max(1, int(self._settings.otp_max_retries or 1))
        max_code_attempts = max(1, int(self._settings.otp_max_retries or 1))
        last_error = ""

        phone_attempt = 1
        while phone_attempt <= max_phone_attempts:
            pooled = self._current_or_new_number(log)
            use_index = pooled.successful_uses + 1
            log(
                f"[SMSBower] 使用号码 {pooled.phone_number} "
                f"(activation={pooled.activation_id}, 第 {use_index}/{self._reuse_limit} 次)"
            )
            send_resp = flow._send_phone_number(http, did, user_agent, ctx, current, pooled.phone_number)
            if send_resp.status_code != 200:
                last_error = flow._phone_submit_error(send_resp)
                log(f"[SMSBower] OpenAI 拒绝当前号码，取消激活后换号: {last_error}")
                self._cancel_current(log)
                if flow._should_retry_phone_number(send_resp, last_error):
                    current = "https://auth.openai.com/add-phone"
                    phone_attempt += 1
                    continue
                raise PhoneVerificationError(f"{label}手机号提交失败 HTTP {send_resp.status_code}: {last_error}")

            current = flow._next_url_from_response(send_resp) or "https://auth.openai.com/phone-verification"
            self._mark_sms_expected(pooled, log)

            for code_attempt in range(1, max_code_attempts + 1):
                try:
                    log(f"[SMSBower] 等待短信验证码 ({code_attempt}/{max_code_attempts})")
                    code = self._client.wait_code(pooled.activation_id).code
                except SmsBowerTimeout as exc:
                    last_error = str(exc)
                    if pooled.successful_uses <= 0 and pooled.received_codes <= 0:
                        log(f"[SMSBower] 新号码首次等码超时，取消当前号码并立即换号: {last_error}")
                        self._cancel_current(log)
                        current = "https://auth.openai.com/add-phone"
                        phone_attempt += 1
                        break
                    if code_attempt >= max_code_attempts:
                        log(f"[SMSBower] 已收过码的号码等码超时且达到重试上限，取消后换号: {last_error}")
                        self._cancel_current(log)
                        current = "https://auth.openai.com/add-phone"
                        phone_attempt += 1
                        break
                    log(f"[SMSBower] 已收过码的号码等待验证码超时，触发 OpenAI 重发并继续等待: {last_error}")
                    self._request_resend(flow, http, did, user_agent, ctx, current, label, pooled, log)
                    continue
                except SmsBowerApiError as exc:
                    last_error = str(exc)
                    log(f"[SMSBower] 激活状态不可继续，取消后换号: {last_error}")
                    self._cancel_current(log)
                    current = "https://auth.openai.com/add-phone"
                    phone_attempt += 1
                    break

                pooled.received_codes += 1
                verify_resp = flow._validate_phone_otp(http, did, user_agent, ctx, current, code)
                if verify_resp.status_code == 200:
                    next_url = flow._next_url_from_response(verify_resp) or current
                    _, final_url = http.follow_redirects(next_url)
                    self._record_success(log)
                    log(f"[SMSBower] {label} 手机号验证完成")
                    return final_url

                last_error = flow._response_text(verify_resp) or str(getattr(verify_resp, "text", "") or "")[:300]
                log(f"[SMSBower] 验证码校验失败: {last_error or f'HTTP {verify_resp.status_code}'}")
                if not flow._should_retry_phone_otp(verify_resp, last_error):
                    self._cancel_current(log)
                    raise PhoneVerificationError(f"{label}手机验证码校验失败 HTTP {verify_resp.status_code}: {last_error}")
                self._request_resend(flow, http, did, user_agent, ctx, current, label, pooled, log)
            else:
                self._cancel_current(log)
                current = "https://auth.openai.com/add-phone"
                phone_attempt += 1
                continue

        raise PhoneVerificationError(f"{label}SMSBower 手机验证失败，已尝试 {max_phone_attempts} 个号码: {last_error}")

    def _current_or_new_number(self, log: SmsBowerLog) -> _PooledActivation:
        if self._active is not None and self._active.successful_uses < self._reuse_limit:
            return self._active
        if self._active is not None:
            self._finish_current(log)
        try:
            activation = self._client.get_number()
        except SmsBowerNoNumber:
            raise
        except SmsBowerApiError:
            raise
        pooled = self._from_activation(activation)
        self._active = pooled
        log(f"[SMSBower] 已租用新号码 {pooled.phone_number} (activation={pooled.activation_id})")
        return pooled

    def _mark_sms_expected(self, pooled: _PooledActivation, log: SmsBowerLog) -> None:
        try:
            if pooled.successful_uses <= 0:
                result = self._client.mark_ready(pooled.activation_id)
                log(f"[SMSBower] setStatus=1 已标记就绪: {result}")
            else:
                result = self._client.request_another_sms(pooled.activation_id)
                log(f"[SMSBower] setStatus=3 已请求下一条短信: {result}")
        except SmsBowerApiError as exc:
            log(f"[SMSBower] 更新激活状态失败，继续等待短信: {exc}")

    def _request_resend(
        self,
        flow: RegisterFlow,
        http: OpenAIHTTP,
        did: str,
        user_agent: str,
        ctx: dict[str, Any],
        current: str,
        label: str,
        pooled: _PooledActivation,
        log: SmsBowerLog,
    ) -> None:
        flow._resend_phone_otp(http, did, user_agent, ctx, current, label)
        try:
            result = self._client.request_another_sms(pooled.activation_id)
            log(f"[SMSBower] setStatus=3 已请求重发短信: {result}")
        except SmsBowerApiError as exc:
            log(f"[SMSBower] 请求下一条短信状态失败，继续按当前激活等待: {exc}")

    def _record_success(self, log: SmsBowerLog) -> None:
        if self._active is None:
            return
        self._active.successful_uses += 1
        if self._active.successful_uses >= self._reuse_limit:
            self._finish_current(log)
            return
        remaining = self._reuse_limit - self._active.successful_uses
        log(f"[SMSBower] 当前号码已成功使用 {self._active.successful_uses} 次，还会复用 {remaining} 次")

    def _finish_current(self, log: SmsBowerLog) -> None:
        active = self._active
        self._active = None
        if active is None:
            return
        try:
            result = self._client.finish_activation(active.activation_id)
            log(f"[SMSBower] 当前号码已完成 {active.successful_uses}/{self._reuse_limit} 次使用，setStatus=6: {result}")
        except SmsBowerApiError as exc:
            log(f"[SMSBower] 完成释放号码失败: {exc}")

    def _cancel_current(self, log: SmsBowerLog) -> None:
        active = self._active
        self._active = None
        if active is None:
            return
        try:
            result = self._client.cancel_activation(active.activation_id)
            log(f"[SMSBower] 当前号码已取消 setStatus=8: {result}")
        except SmsBowerApiError as exc:
            log(f"[SMSBower] 取消号码失败: {exc}")

    def _lease_slot(self, log: SmsBowerLog) -> "_SmsBowerLease":
        return _SmsBowerLease(self._condition, self, log)

    @staticmethod
    def _from_activation(activation: SmsBowerActivation) -> _PooledActivation:
        return _PooledActivation(activation_id=activation.activation_id, phone_number=activation.phone_number)


class _SmsBowerLease:
    def __init__(self, condition: threading.Condition, pool: SmsBowerPhonePool, log: SmsBowerLog):
        self._condition = condition
        self._pool = pool
        self._log = log

    def __enter__(self) -> None:
        with self._condition:
            if self._pool._busy:
                self._log("[SMSBower] 其他任务正在使用接码号码，当前任务等待复用队列")
            while self._pool._busy:
                self._condition.wait()
            self._pool._busy = True

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        with self._condition:
            self._pool._busy = False
            self._condition.notify_all()
