from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import sqlite3
import socket
import sys
import threading
import time
import webbrowser
from typing import Any

from curl_cffi import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from .config import AppConfig, load_app_config, save_airgate_monitor_config
from .airgate_monitor import AirGate401Monitor, AirGateMonitorConfig
from .flow import RegisterFlow
from .proxy_pool import pick_proxy_from_pool
from .settings import Settings, proxy_preview, resolve_proxy_pool
from .storage import (
    ACCOUNT_STATUS_ABANDONED,
    NULL_VALUE,
    AUTH_ROLE_ADMIN,
    AUTH_ROLE_OPERATOR,
    AUTH_STATUS_ACTIVE,
    STOCK_STATUS_IN,
    STOCK_STATUS_OUT,
    SUBSCRIPTION_STATUS_CLAIMED,
    SUBSCRIPTION_STATUS_FAILED,
    SUBSCRIPTION_STATUS_MARKED,
    SUBSCRIPTION_STATUS_PENDING,
    SUBSCRIPTION_STATUS_VERIFIED,
    WEB_SESSION_TTL_SECONDS,
    build_airgate_accounts_json,
    build_accounts_db_txt,
    build_checkout_db_jsonl,
    build_tokens_db_jsonl,
    account_stats_db,
    authenticate_web_user,
    claim_subscription_account_db,
    count_account_rows,
    delete_account_db,
    delete_web_user,
    init_accounts_db,
    get_account_db,
    list_due_subscription_verification_accounts_db,
    list_account_rows,
    list_account_rows_by_ids,
    list_subscription_queue_db,
    mark_subscription_account_clicked_db,
    mark_subscription_account_failed_db,
    release_subscription_account_db,
    list_web_users,
    save_account_storage,
    save_account_db_record,
    save_authorization_token_db,
    save_login_session_db,
    create_web_session,
    create_web_user,
    ensure_bootstrap_admin_user,
    record_audit_log,
    resolve_web_session,
    revoke_web_session,
    revoke_web_sessions_for_user,
    set_web_user_password,
    verify_subscription_account_db,
    try_load_login_session_db,
    update_web_user,
    update_account_checkout_url_db,
    update_account_status_db,
    update_account_subscription_type_db,
    update_account_stock_status_db,
    utc_now,
    update_subscription_verification_tracking_db,
)
from .utils import make_password, make_random_email


class AccountPayload(BaseModel):
    email: str
    password: str
    subscription_type: str = NULL_VALUE
    refresh_token: str = NULL_VALUE
    session_json: str = NULL_VALUE
    checkout_url: str = NULL_VALUE
    status: str = "active"
    stock_status: str = STOCK_STATUS_IN


class StockStatusPayload(BaseModel):
    stock_status: str


class BatchIdsPayload(BaseModel):
    ids: list[int]


class BatchStockStatusPayload(BatchIdsPayload):
    stock_status: str


class OperationPayload(BaseModel):
    mode: str
    email: str = ""
    password: str = ""
    account_id: int | None = None
    generate_email: bool = False
    generate_password: bool = False
    create_checkout: bool = False


class BatchOperationPayload(OperationPayload):
    count: int = 1


class AutoRegisterPayload(BaseModel):
    interval_seconds: int = 300
    batch_count: int = 1
    create_checkout: bool = True


class AirGateMonitorPayload(BaseModel):
    core_url: str = ""
    admin_key: str = ""
    proxy: str = ""
    poll_interval_seconds: int = 300
    account_cooldown_seconds: int = 1800
    page_size: int = 100


class PromptPayload(BaseModel):
    value: str


class LoginPayload(BaseModel):
    username: str
    password: str


class OperatorPayload(BaseModel):
    username: str
    password: str
    display_name: str = ""
    permissions: list[str] | None = None
    status: str = AUTH_STATUS_ACTIVE


class UserUpdatePayload(BaseModel):
    display_name: str | None = None
    role: str | None = None
    permissions: list[str] | None = None
    status: str | None = None
    must_change_password: bool | None = None


class ResetPasswordPayload(BaseModel):
    password: str
    must_change_password: bool = False


class SubscriptionActionPayload(BaseModel):
    note: str = ""


class ClaimPayload(BaseModel):
    claim_minutes: int = 30


WEB_SESSION_COOKIE = "protocol_reg_session"


@dataclass(frozen=True)
class WebRuntime:
    repo_root: Path
    config_path: Path
    license_file: Path | None
    proxy: str = ""
    login_delay: int = 20
    timeout: int = 30
    ssl_verify: bool = True
    max_concurrency: int = 3


@dataclass
class WebJob:
    id: str
    mode: str
    email: str
    proxy: str = ""
    status: str = "pending"
    queue_position: int = 0
    prompt: str = ""
    error: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    logs: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    _condition: threading.Condition = field(default_factory=threading.Condition)
    _input_value: str | None = None

    def log(self, message: object) -> None:
        text = str(message).rstrip()
        if not text:
            return
        with self._condition:
            for line in text.splitlines():
                self.logs.append(line)
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]
            self.updated_at = time.time()
            self._condition.notify_all()

    def set_status(self, status: str, *, queue_position: int | None = None) -> None:
        with self._condition:
            self.status = status
            if queue_position is not None:
                self.queue_position = max(0, int(queue_position))
            self.updated_at = time.time()
            self._condition.notify_all()

    def set_result(self, result: dict[str, Any]) -> None:
        with self._condition:
            self.result = result
            self.updated_at = time.time()
            self._condition.notify_all()

    def set_error(self, error: str) -> None:
        with self._condition:
            self.error = error
            self.updated_at = time.time()
            self._condition.notify_all()

    def wait_input(self, prompt: str) -> str:
        with self._condition:
            self.prompt = prompt
            self.status = "waiting"
            self.queue_position = 0
            self.updated_at = time.time()
            self.logs.append(f"[等待输入] {prompt}")
            self._condition.notify_all()
            while self._input_value is None:
                self._condition.wait(timeout=1)
            value = self._input_value
            self._input_value = None
            self.prompt = ""
            self.status = "running"
            self.updated_at = time.time()
            return value

    def submit_input(self, value: str) -> None:
        with self._condition:
            self._input_value = value
            self.logs.append("[输入] 已收到页面提交，继续执行")
            self.updated_at = time.time()
            self._condition.notify_all()


class JobWriter:
    def __init__(self, job: WebJob):
        self.job = job
        self._buffer = ""
        self._terminal_buffer = ""

    def write(self, data: str) -> int:
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self.job.log(line)
        return len(data)

    def flush(self) -> None:
        if self._buffer:
            self.job.log(self._buffer)
            self._buffer = ""

    def write_terminal(self, fallback: Any, data: str) -> int:
        self._terminal_buffer += data
        while "\n" in self._terminal_buffer:
            line, self._terminal_buffer = self._terminal_buffer.split("\n", 1)
            self._write_terminal_line(fallback, line.rstrip("\r"))
        return len(data)

    def flush_terminal(self, fallback: Any) -> None:
        if self._terminal_buffer:
            self._write_terminal_line(fallback, self._terminal_buffer.rstrip("\r"))
            self._terminal_buffer = ""

    def _write_terminal_line(self, fallback: Any, line: str) -> None:
        fallback.write(f"{self._terminal_prefix()} {line}\n")

    def _terminal_prefix(self) -> str:
        email = self.job.email.strip() or "未命名"
        return f"[任务 {self.job.id[:8]} {self.job.mode} {email}]"


class _ThreadBoundStream:
    def __init__(self, fallback: Any):
        self._fallback = fallback
        self._local = threading.local()

    @contextmanager
    def bind(self, writer: JobWriter):
        previous = getattr(self._local, "writer", None)
        self._local.writer = writer
        try:
            yield
        finally:
            if previous is None:
                if hasattr(self._local, "writer"):
                    delattr(self._local, "writer")
            else:
                self._local.writer = previous

    def write(self, data: str) -> int:
        writer = getattr(self._local, "writer", None)
        if writer is None:
            return self._fallback.write(data)
        written = writer.write(data)
        try:
            with _stream_router_lock:
                writer.write_terminal(self._fallback, data)
                self._fallback.flush()
        except Exception:
            pass
        return written

    def flush(self) -> None:
        writer = getattr(self._local, "writer", None)
        if writer is None:
            self._fallback.flush()
            return
        writer.flush()
        try:
            with _stream_router_lock:
                writer.flush_terminal(self._fallback)
                self._fallback.flush()
        except Exception:
            pass

    def isatty(self) -> bool:
        return bool(getattr(self._fallback, "isatty", lambda: False)())

    def __getattr__(self, name: str) -> Any:
        return getattr(self._fallback, name)


_stdout_router: _ThreadBoundStream | None = None
_stderr_router: _ThreadBoundStream | None = None
_stream_router_lock = threading.Lock()
_web_file_lock = threading.RLock()


def _bind_job_output(writer: JobWriter):
    global _stdout_router, _stderr_router
    with _stream_router_lock:
        if not isinstance(sys.stdout, _ThreadBoundStream):
            _stdout_router = _ThreadBoundStream(sys.stdout)
            sys.stdout = _stdout_router
        else:
            _stdout_router = sys.stdout
        if not isinstance(sys.stderr, _ThreadBoundStream):
            _stderr_router = _ThreadBoundStream(sys.stderr)
            sys.stderr = _stderr_router
        else:
            _stderr_router = sys.stderr
    return _stdout_router.bind(writer), _stderr_router.bind(writer)


class JobManager:
    def __init__(self, runtime: WebRuntime):
        self.runtime = runtime
        self.max_concurrency = max(1, int(runtime.max_concurrency or 1))
        self._jobs: dict[str, WebJob] = {}
        self._queue: list[tuple[WebJob, OperationPayload, str]] = []
        self._running_ids: set[str] = set()
        self._lock = threading.RLock()

    def start(self, payload: OperationPayload) -> WebJob:
        mode = payload.mode.strip().lower()
        if mode not in {"register", "login", "authorize"}:
            raise ValueError("运行模式必须是 register、login 或 authorize")
        cfg = load_app_config(self.runtime.config_path)
        with self._lock:
            email, password = _resolve_operation_account(
                payload,
                cfg,
                self.runtime,
                reserved_emails=self._reserved_register_emails_locked(),
            )
            resolved_payload = payload.model_copy(
                update={"mode": mode, "email": email, "password": password},
            )
            proxy, proxy_count = self._next_proxy_locked(cfg)
            job = WebJob(id=secrets.token_hex(8), mode=mode, email=email, proxy=proxy)
            self._jobs[job.id] = job
            self._queue.append((job, resolved_payload, proxy))
            self._refresh_queue_positions_locked()
            job.log(f"[队列] 已入队，排队位置 {job.queue_position}，并发上限 {self.max_concurrency}")
            if proxy_count > 1:
                job.log(f"[代理] 轮询选择 {proxy_preview(proxy)}（代理池 {proxy_count} 个）")
            elif proxy:
                job.log(f"[代理] 使用 {proxy_preview(proxy)}")
            to_start = self._schedule_locked()
        self._start_workers(to_start)
        return job

    def start_many(self, payload: OperationPayload, count: int) -> list[WebJob]:
        total = max(1, int(count))
        return [self.start(payload) for _ in range(total)]

    def get(self, job_id: str) -> WebJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self) -> list[WebJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)[:100]

    def stats(self) -> dict[str, int]:
        with self._lock:
            statuses = [item.status for item in self._jobs.values()]
            pending = statuses.count("pending")
            running = statuses.count("running")
            waiting = statuses.count("waiting")
            return {
                "max_concurrency": self.max_concurrency,
                "pending": pending,
                "running": running,
                "waiting": waiting,
                "active": pending + running + waiting,
                "stored": len(self._jobs),
            }

    def _reserved_register_emails_locked(self) -> set[str]:
        return {
            job.email.strip().lower()
            for job in self._jobs.values()
            if job.mode == "register"
            and job.status in {"pending", "running", "waiting"}
            and job.email.strip()
        }

    def _refresh_queue_positions_locked(self) -> None:
        for index, (job, _, _) in enumerate(self._queue, start=1):
            job.set_status("pending", queue_position=index)

    def _schedule_locked(self) -> list[tuple[WebJob, OperationPayload, str]]:
        to_start: list[tuple[WebJob, OperationPayload, str]] = []
        while len(self._running_ids) < self.max_concurrency and self._queue:
            job, payload, proxy = self._queue.pop(0)
            self._running_ids.add(job.id)
            job.set_status("running", queue_position=0)
            job.log(f"[队列] 获得执行槽，当前运行 {len(self._running_ids)}/{self.max_concurrency}")
            to_start.append((job, payload, proxy))
        self._refresh_queue_positions_locked()
        return to_start

    def _start_workers(self, jobs: list[tuple[WebJob, OperationPayload, str]]) -> None:
        for job, payload, proxy in jobs:
            thread = threading.Thread(target=self._run, args=(job, payload, proxy), daemon=True)
            thread.start()

    def _run(self, job: WebJob, payload: OperationPayload, proxy: str) -> None:
        writer = JobWriter(job)
        stdout_ctx, stderr_ctx = _bind_job_output(writer)
        with stdout_ctx, stderr_ctx:
            flow: RegisterFlow | None = None
            try:
                settings = _settings_from_runtime(self.runtime, proxy=proxy)
                email = payload.email.strip().lower()
                password = payload.password
                if payload.mode.strip().lower() == "register" and _email_exists(email):
                    raise ValueError(f"邮箱已存在于数据库，避免重复注册: {email}")
                job.email = email
                job.log(f"[任务] 开始执行 {payload.mode}: {email}")
                flow = RegisterFlow(settings, prompt=job.wait_input)
                result = _execute_operation(job, payload, settings, flow, email, password, self.runtime)
                job.set_result(result)
                job.set_status("succeeded")
                job.log("[任务] 执行完成")
            except Exception as exc:
                job.set_error(str(exc))
                job.set_status("failed")
                job.log(f"[错误] {exc}")
            finally:
                if flow is not None:
                    flow.close()
                writer.flush()
        if job.status == "failed":
            self._archive_failed_job(job)
        with self._lock:
            self._running_ids.discard(job.id)
            self._prune_jobs_locked()
            to_start = self._schedule_locked()
        self._start_workers(to_start)

    def _prune_jobs_locked(self) -> None:
        completed = [
            job
            for job in self._jobs.values()
            if job.status in {"succeeded", "failed"}
        ]
        if len(self._jobs) <= 120:
            return
        for job in sorted(completed, key=lambda item: item.updated_at)[: len(self._jobs) - 120]:
            self._jobs.pop(job.id, None)

    def _next_proxy_locked(self, cfg: AppConfig) -> tuple[str, int]:
        pool = _resolve_runtime_proxy_pool(self.runtime, cfg)
        return pick_proxy_from_pool(pool), len(pool)

    def _archive_failed_job(self, job: WebJob) -> None:
        try:
            log_path = self.runtime.repo_root / "data" / "failed_jobs.log"
            entry = {
                "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(job.updated_at)),
                "job_id": job.id,
                "mode": job.mode,
                "email": job.email,
                "error": job.error,
                "logs": list(job.logs[-20:]),
            }
            with _web_file_lock:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False))
                    handle.write("\n")
        except Exception as exc:
            print(f"[任务] 失败记录写入失败: {exc}", file=sys.stderr)


class AutoRegisterScheduler:
    def __init__(self, manager: JobManager):
        self.manager = manager
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = False
        self._interval_seconds = 300
        self._batch_count = 1
        self._create_checkout = True
        self._run_count = 0
        self._last_run_at: float | None = None
        self._next_run_at: float | None = None
        self._last_error = ""
        self._last_job_ids: list[str] = []

    def start(self, payload: AutoRegisterPayload) -> dict[str, Any]:
        interval = max(1, int(payload.interval_seconds or 1))
        batch_count = max(1, min(20, int(payload.batch_count or 1)))
        with self._lock:
            was_enabled = self._enabled
            self._interval_seconds = interval
            self._batch_count = batch_count
            self._create_checkout = bool(payload.create_checkout)
            self._enabled = True
            self._last_error = ""
            self._next_run_at = time.time() + interval if was_enabled else time.time()
            if self._thread is None or not self._thread.is_alive():
                self._wake.clear()
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
            else:
                self._wake.set()
            return self.status()

    def configure(self, payload: AutoRegisterPayload) -> dict[str, Any]:
        interval = max(1, int(payload.interval_seconds or 1))
        batch_count = max(1, min(20, int(payload.batch_count or 1)))
        with self._lock:
            self._interval_seconds = interval
            self._batch_count = batch_count
            self._create_checkout = bool(payload.create_checkout)
            self._last_error = ""
            if self._enabled:
                self._next_run_at = time.time() + interval
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
                "interval_seconds": self._interval_seconds,
                "batch_count": self._batch_count,
                "create_checkout": self._create_checkout,
                "run_count": self._run_count,
                "last_run_at": self._last_run_at,
                "next_run_at": self._next_run_at,
                "last_error": self._last_error,
                "last_job_ids": self._last_job_ids[-20:],
            }

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
                self._next_run_at = time.time() + self._interval_seconds

    def _run_once(self) -> None:
        with self._lock:
            batch_count = self._batch_count
            create_checkout = self._create_checkout
        payload = OperationPayload(
            mode="register",
            email="",
            password="",
            account_id=None,
            generate_email=True,
            generate_password=True,
            create_checkout=create_checkout,
        )
        try:
            jobs = self.manager.start_many(payload, batch_count)
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._last_run_at = time.time()
            return
        with self._lock:
            self._run_count += 1
            self._last_run_at = time.time()
            self._last_error = ""
            self._last_job_ids = [job.id for job in jobs]


class SubscriptionVerifyScheduler:
    def __init__(self, runtime: WebRuntime):
        self.runtime = runtime
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._enabled = False
        self._poll_interval_seconds = 3
        self._batch_size = 12
        self._max_attempts = 12
        self._run_count = 0
        self._last_run_at: float | None = None
        self._next_run_at: float | None = None
        self._last_error = ""
        self._last_account_ids: list[str] = []
        self._last_processed_count = 0

    def start(self) -> dict[str, Any]:
        with self._lock:
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

    def stop(self) -> dict[str, Any]:
        with self._lock:
            self._enabled = False
            self._next_run_at = None
            self._wake.set()
            return self.status()

    def run_soon(self) -> None:
        with self._lock:
            if not self._enabled:
                return
            self._next_run_at = time.time()
            self._wake.set()

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self._enabled,
                "poll_interval_seconds": self._poll_interval_seconds,
                "batch_size": self._batch_size,
                "max_attempts": self._max_attempts,
                "run_count": self._run_count,
                "last_run_at": self._last_run_at,
                "next_run_at": self._next_run_at,
                "last_error": self._last_error,
                "last_account_ids": self._last_account_ids[-20:],
                "last_processed_count": self._last_processed_count,
            }

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
                self._next_run_at = time.time() + self._poll_interval_seconds

    def _run_once(self) -> None:
        with self._lock:
            batch_size = self._batch_size
            max_attempts = self._max_attempts
        try:
            accounts = list_due_subscription_verification_accounts_db(
                limit=batch_size,
                max_attempts=max_attempts,
            )
        except Exception as exc:
            with self._lock:
                self._last_error = str(exc)
                self._last_run_at = time.time()
                self._last_processed_count = 0
            return
        processed_ids: list[str] = []
        last_error = ""
        for account in accounts:
            account_id = str(account.get("id") or "")
            if not account_id:
                continue
            try:
                self._process_account(account)
                processed_ids.append(account_id)
            except Exception as exc:
                last_error = str(exc)
        with self._lock:
            self._run_count += 1
            self._last_run_at = time.time()
            self._last_error = last_error
            self._last_account_ids = processed_ids
            self._last_processed_count = len(processed_ids)

    def _process_account(self, account: dict[str, Any]) -> None:
        account_id = int(account.get("id") or 0)
        if account_id <= 0:
            return
        current_attempts = max(0, int(account.get("subscription_verify_attempts") or 0))
        next_attempt = current_attempts + 1
        now = utc_now()
        try:
            refreshed = _refresh_account_subscription_type(
                account_id,
                self.runtime,
                proxy=_next_proxy_for_runtime(self.runtime),
            )
            plan_type = str(refreshed.get("subscription_type") or NULL_VALUE).strip() or NULL_VALUE
        except Exception as exc:
            message = f"核实失败：{exc}"
            next_check_at = _utc_after_seconds(_subscription_verify_delay_seconds(next_attempt))
            update_subscription_verification_tracking_db(
                account_id,
                attempts=next_attempt,
                last_checked_at=now,
                next_check_at=next_check_at,
                last_message=message,
            )
            return
        if _is_paid_subscription_plan(plan_type):
            verify_subscription_account_db(account_id)
            update_subscription_verification_tracking_db(
                account_id,
                attempts=next_attempt,
                last_checked_at=now,
                next_check_at=NULL_VALUE,
                last_message=f"已确认订阅 · {plan_type}",
            )
            record_audit_log(
                None,
                "auto_verify_subscription",
                target_type="account",
                target_id=str(account_id),
                detail={
                    "attempts": next_attempt,
                    "plan_type": plan_type,
                    "status": "verified",
                },
            )
            return
        message = f"当前订阅类型为 {plan_type}"
        if next_attempt >= self._max_attempts:
            update_subscription_verification_tracking_db(
                account_id,
                attempts=next_attempt,
                last_checked_at=now,
                next_check_at=NULL_VALUE,
                last_message=f"自动核实已暂停：{message}",
            )
            record_audit_log(
                None,
                "auto_verify_subscription_stopped",
                target_type="account",
                target_id=str(account_id),
                detail={
                    "attempts": next_attempt,
                    "plan_type": plan_type,
                    "status": "stopped",
                    "message": message,
                },
            )
            return
        next_check_at = _utc_after_seconds(_subscription_verify_delay_seconds(next_attempt))
        update_subscription_verification_tracking_db(
            account_id,
            attempts=next_attempt,
            last_checked_at=now,
            next_check_at=next_check_at,
            last_message=message,
        )


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def create_app(runtime: WebRuntime | None = None) -> FastAPI:
    repo_root = _repo_root()
    if runtime is None:
        runtime = WebRuntime(
            repo_root=repo_root,
            config_path=(repo_root / "config" / "protocol-reg.yaml").resolve(),
            license_file=_default_license_file(repo_root),
        )
    app_cfg = load_app_config(runtime.config_path)
    manager = JobManager(runtime)
    auto_scheduler = AutoRegisterScheduler(manager)
    subscription_verify_scheduler = SubscriptionVerifyScheduler(runtime)
    airgate_monitor = AirGate401Monitor(
        app_cfg.airgate_monitor,
        lambda: _settings_from_runtime(runtime, proxy=_airgate_monitor_proxy(airgate_monitor)),
    )

    app = FastAPI(title="Protocol Reg 账号管理", version="0.1.0")
    app.state.bootstrap_admin = ensure_bootstrap_admin_user()
    app.state.airgate_monitor = airgate_monitor

    def _current_user_from_request(request: Request) -> dict[str, Any] | None:
        current = getattr(request.state, "current_user", None)
        if isinstance(current, dict) and current.get("id"):
            return current
        token = str(request.cookies.get(WEB_SESSION_COOKIE) or "").strip()
        if not token:
            return None
        session = resolve_web_session(token)
        if session is None:
            return None
        request.state.current_user = session
        return session

    def _require_current_user(request: Request) -> dict[str, Any]:
        user = _current_user_from_request(request)
        if user is None:
            raise HTTPException(status_code=401, detail="未登录")
        return user

    def _require_admin_user(request: Request) -> dict[str, Any]:
        user = _require_current_user(request)
        role = str(user.get("role") or "").strip().lower()
        if role != AUTH_ROLE_ADMIN:
            raise HTTPException(status_code=403, detail="需要管理员权限")
        return user

    def _require_operator_user(request: Request) -> dict[str, Any]:
        user = _require_current_user(request)
        role = str(user.get("role") or "").strip().lower()
        if role not in {AUTH_ROLE_ADMIN, AUTH_ROLE_OPERATOR}:
            raise HTTPException(status_code=403, detail="需要操作员权限")
        return user

    def _has_operator_permission(user: dict[str, Any], permission: str) -> bool:
        role = str(user.get("role") or "").strip().lower()
        if role == AUTH_ROLE_ADMIN:
            return True
        permissions = user.get("permissions")
        if not isinstance(permissions, list):
            return False
        normalized = {str(item).strip() for item in permissions if str(item or "").strip()}
        return "*" in normalized or str(permission).strip() in normalized

    def _require_operator_permission(request: Request, permission: str) -> dict[str, Any]:
        user = _require_operator_user(request)
        if not _has_operator_permission(user, permission):
            raise HTTPException(status_code=403, detail="当前账号没有该操作权限")
        return user

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        path = request.url.path
        if path == "/login":
            user = _current_user_from_request(request)
            if user is not None:
                role = str(user.get("role") or "").strip().lower()
                target = "/operator" if role == AUTH_ROLE_OPERATOR else "/"
                return RedirectResponse(url=target, status_code=303)
            return await call_next(request)
        public_paths = {"/login", "/api/auth/login", "/api/auth/logout", "/api/auth/bootstrap"}
        if path in public_paths:
            response = await call_next(request)
            return response
        user = _current_user_from_request(request)
        if user is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "未登录或会话已过期"}, status_code=401)
            return RedirectResponse(url="/login", status_code=303)
        if path == "/api/auth/me":
            return await call_next(request)
        role = str(user.get("role") or "").strip().lower()
        if path.startswith("/api/admin") or path.startswith("/admin"):
            if role != AUTH_ROLE_ADMIN:
                return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
        elif path.startswith("/api/operator") or path.startswith("/operator"):
            if role not in {AUTH_ROLE_ADMIN, AUTH_ROLE_OPERATOR}:
                return JSONResponse({"detail": "需要操作员权限"}, status_code=403)
        else:
            if role != AUTH_ROLE_ADMIN:
                if path.startswith("/api/"):
                    return JSONResponse({"detail": "需要管理员权限"}, status_code=403)
                return RedirectResponse(url="/operator", status_code=303)
        if path == "/login":
            return RedirectResponse(url="/", status_code=303)
        response = await call_next(request)
        return response

    @app.on_event("startup")
    def startup() -> None:
        init_accounts_db()
        subscription_verify_scheduler.start()
        if app_cfg.airgate_monitor.enabled and app_cfg.airgate_monitor.core_url.strip() and app_cfg.airgate_monitor.admin_key.strip():
            airgate_monitor.start(app_cfg.airgate_monitor)

    @app.on_event("shutdown")
    def shutdown() -> None:
        auto_scheduler.stop()
        subscription_verify_scheduler.stop()
        airgate_monitor.stop()

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> str:
        return _render_login_page()

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _render_html_page("accounts")

    @app.get("/tasks", response_class=HTMLResponse)
    def tasks_page() -> str:
        return _render_html_page("tasks")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page() -> str:
        return _render_html_page("settings")

    @app.get("/admin/users", response_class=HTMLResponse)
    def admin_users_page() -> str:
        return _render_admin_users_page()

    @app.get("/api/auth/me")
    def api_auth_me(request: Request) -> dict[str, Any]:
        user = _current_user_from_request(request)
        if user is None:
            raise HTTPException(status_code=401, detail="未登录")
        return {"user": _public_web_user(user)}

    @app.post("/api/auth/login")
    def api_auth_login(payload: LoginPayload, request: Request) -> dict[str, Any]:
        ip = _request_ip(request)
        user_agent = str(request.headers.get("user-agent") or "")
        user = authenticate_web_user(payload.username, payload.password, ip=ip, user_agent=user_agent)
        if user is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        session = create_web_session(int(user["id"]), ip=ip, user_agent=user_agent, ttl_seconds=WEB_SESSION_TTL_SECONDS)
        record_audit_log(
            int(user["id"]),
            "login",
            target_type="session",
            target_id=session.get("session_id", ""),
            detail={"username": user["username"]},
            ip=ip,
            user_agent=user_agent,
        )
        response = {"user": _public_web_user(user), "expires_at": session["expires_at"]}
        http_response = JSONResponse(response)
        http_response.set_cookie(
            WEB_SESSION_COOKIE,
            session["token"],
            max_age=WEB_SESSION_TTL_SECONDS,
            httponly=True,
            samesite="lax",
            secure=False,
            path="/",
        )
        return http_response

    @app.post("/api/auth/logout")
    def api_auth_logout(request: Request) -> dict[str, Any]:
        token = str(request.cookies.get(WEB_SESSION_COOKIE) or "").strip()
        user = _current_user_from_request(request)
        if token:
            revoke_web_session(token)
        if user is not None:
            record_audit_log(int(user["id"]), "logout", target_type="session", target_id=user.get("session_id", ""), ip=_request_ip(request), user_agent=str(request.headers.get("user-agent") or ""))
        response = JSONResponse({"ok": True})
        response.delete_cookie(WEB_SESSION_COOKIE, path="/")
        return response

    @app.get("/api/admin/users")
    def api_list_web_users(request: Request) -> dict[str, Any]:
        _require_admin_user(request)
        return {"items": [_public_web_user(user) for user in list_web_users()]}

    @app.post("/api/admin/users")
    def api_create_web_user(payload: OperatorPayload, request: Request) -> dict[str, Any]:
        actor = _require_admin_user(request)
        try:
            user = create_web_user(
                username=payload.username,
                password=payload.password,
                role=AUTH_ROLE_OPERATOR,
                display_name=payload.display_name,
                permissions=payload.permissions,
                status=payload.status,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        record_audit_log(int(actor["id"]), "create_user", target_type="web_user", target_id=user["id"], detail={"username": user["username"], "role": user["role"]}, ip=_request_ip(request), user_agent=str(request.headers.get("user-agent") or ""))
        return {"item": _public_web_user(user), "items": [_public_web_user(item) for item in list_web_users()]}

    @app.patch("/api/admin/users/{user_id}")
    def api_update_web_user(user_id: int, payload: UserUpdatePayload, request: Request) -> dict[str, Any]:
        actor = _require_admin_user(request)
        current = next((item for item in list_web_users() if str(item.get("id")) == str(user_id)), None)
        if current is not None:
            requested_role = str(payload.role or current.get("role") or AUTH_ROLE_OPERATOR).strip().lower()
            if payload.role is not None and requested_role != str(current.get("role") or AUTH_ROLE_OPERATOR).strip().lower():
                raise HTTPException(status_code=400, detail="不支持通过页面修改用户角色")
            requested_status = str(payload.status or current.get("status") or AUTH_STATUS_ACTIVE).strip().lower()
            if (
                str(current.get("role") or "").strip().lower() == AUTH_ROLE_ADMIN
                and requested_status != AUTH_STATUS_ACTIVE
            ):
                other_active_admins = [
                    item
                    for item in list_web_users()
                    if str(item.get("id") or "") != str(user_id)
                    and str(item.get("role") or "").strip().lower() == AUTH_ROLE_ADMIN
                    and str(item.get("status") or "").strip().lower() == AUTH_STATUS_ACTIVE
                ]
                if not other_active_admins:
                    raise HTTPException(status_code=400, detail="至少保留一个启用的管理员")
        try:
            user = update_web_user(
                user_id,
                display_name=payload.display_name,
                role=None,
                permissions=payload.permissions,
                status=payload.status,
                must_change_password=payload.must_change_password,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if str(user.get("status") or "").strip().lower() != AUTH_STATUS_ACTIVE:
            revoke_web_sessions_for_user(user_id)
        record_audit_log(int(actor["id"]), "update_user", target_type="web_user", target_id=str(user_id), detail={"username": user["username"], "role": user["role"]}, ip=_request_ip(request), user_agent=str(request.headers.get("user-agent") or ""))
        return {"item": _public_web_user(user), "items": [_public_web_user(item) for item in list_web_users()]}

    @app.post("/api/admin/users/{user_id}/reset-password")
    def api_reset_web_user_password(user_id: int, payload: ResetPasswordPayload, request: Request) -> dict[str, Any]:
        actor = _require_admin_user(request)
        try:
            user = set_web_user_password(user_id, payload.password, must_change_password=payload.must_change_password)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        revoke_web_sessions_for_user(user_id)
        record_audit_log(int(actor["id"]), "reset_password", target_type="web_user", target_id=str(user_id), detail={"username": user["username"]}, ip=_request_ip(request), user_agent=str(request.headers.get("user-agent") or ""))
        return {"item": _public_web_user(user)}

    @app.delete("/api/admin/users/{user_id}")
    def api_delete_web_user(user_id: int, request: Request) -> dict[str, Any]:
        actor = _require_admin_user(request)
        if str(actor.get("id") or "") == str(user_id):
            raise HTTPException(status_code=400, detail="不能删除当前登录账号")
        users = list_web_users()
        current = next((item for item in users if str(item.get("id")) == str(user_id)), None)
        if current is None:
            raise HTTPException(status_code=404, detail=f"用户不存在: {user_id}")
        if str(current.get("role") or "").strip().lower() == AUTH_ROLE_ADMIN:
            other_active_admins = [
                item
                for item in users
                if str(item.get("id") or "") != str(user_id)
                and str(item.get("role") or "").strip().lower() == AUTH_ROLE_ADMIN
                and str(item.get("status") or "").strip().lower() == AUTH_STATUS_ACTIVE
            ]
            if not other_active_admins:
                raise HTTPException(status_code=400, detail="至少保留一个启用的管理员")
        try:
            user = delete_web_user(user_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        record_audit_log(int(actor["id"]), "delete_user", target_type="web_user", target_id=str(user_id), detail={"username": user["username"], "role": user["role"]}, ip=_request_ip(request), user_agent=str(request.headers.get("user-agent") or ""))
        return {"item": _public_web_user(user), "items": [_public_web_user(item) for item in list_web_users()]}

    @app.get("/operator", response_class=HTMLResponse)
    def operator_page() -> str:
        return _render_operator_page()

    @app.get("/api/operator/subscriptions")
    def api_operator_subscriptions(
        request: Request,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=200),
    ) -> dict[str, Any]:
        user = _require_operator_permission(request, "view_subscription_accounts")
        operator_id = int(user["id"]) if str(user.get("role") or "").strip().lower() == AUTH_ROLE_OPERATOR else None
        operator_labels = _operator_label_map()
        items = list_subscription_queue_db(page=page, page_size=page_size, operator_user_id=operator_id)
        rendered_items = []
        for item in items:
            expose_checkout = operator_id is None or str(item.get("subscription_operator_id") or "") == str(operator_id)
            rendered_items.append(_subscription_queue_item(item, expose_checkout_url=expose_checkout, operator_labels=operator_labels))
        return {
            "items": rendered_items,
            "stats": account_stats_db(),
        }

    @app.post("/api/operator/subscriptions/{account_id}/claim")
    def api_operator_claim_subscription(account_id: int, payload: ClaimPayload, request: Request) -> dict[str, Any]:
        user = _require_operator_permission(request, "claim_subscription_account")
        try:
            saved = claim_subscription_account_db(account_id, int(user["id"]), claim_minutes=payload.claim_minutes)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        record_audit_log(int(user["id"]), "claim_subscription", target_type="account", target_id=str(account_id), detail={"status": saved.get("subscription_status")}, ip=_request_ip(request), user_agent=str(request.headers.get("user-agent") or ""))
        return {"item": _subscription_queue_item(saved, expose_checkout_url=True, operator_labels=_operator_label_map()), "stats": account_stats_db()}

    @app.post("/api/operator/subscriptions/{account_id}/mark-subscribed")
    def api_operator_mark_subscribed(account_id: int, payload: SubscriptionActionPayload, request: Request) -> dict[str, Any]:
        user = _require_operator_permission(request, "mark_subscription_done")
        try:
            saved = mark_subscription_account_clicked_db(account_id, int(user["id"]), note=payload.note)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        plan_type = str(saved.get("subscription_type") or NULL_VALUE).strip() or NULL_VALUE
        verification_message = ""
        try:
            saved, plan_type, verification_error = _try_auto_verify_subscription_account(
                account_id,
                runtime,
                proxy=_next_proxy_for_runtime(runtime),
            )
            verification_message = verification_error or ""
            if verification_error:
                saved = update_subscription_verification_tracking_db(
                    account_id,
                    attempts=1,
                    last_checked_at=_utc_after_seconds(0),
                    next_check_at=_utc_after_seconds(_subscription_verify_delay_seconds(1)),
                    last_message=verification_message,
                )
            else:
                saved = update_subscription_verification_tracking_db(
                    account_id,
                    attempts=1,
                    last_checked_at=_utc_after_seconds(0),
                    next_check_at=NULL_VALUE,
                    last_message=f"已确认订阅 · {plan_type}",
                )
        except Exception as exc:
            verification_message = str(exc)
            saved = get_account_db(account_id) or saved
            saved = update_subscription_verification_tracking_db(
                account_id,
                attempts=1,
                last_checked_at=_utc_after_seconds(0),
                next_check_at=_utc_after_seconds(_subscription_verify_delay_seconds(1)),
                last_message=f"核实失败：{verification_message}",
            )
        auto_verified = not bool(verification_message)
        if not auto_verified:
            subscription_verify_scheduler.run_soon()
        detail = {"note": payload.note, "auto_verified": auto_verified, "plan_type": plan_type}
        if verification_message:
            detail["verification_message"] = verification_message
        record_audit_log(int(user["id"]), "mark_subscribed", target_type="account", target_id=str(account_id), detail=detail, ip=_request_ip(request), user_agent=str(request.headers.get("user-agent") or ""))
        record_audit_log(
            int(user["id"]),
            "auto_verify_subscription",
            target_type="account",
            target_id=str(account_id),
            detail={
                "plan_type": plan_type,
                "success": auto_verified,
                "verification_message": verification_message if verification_message else NULL_VALUE,
            },
            ip=_request_ip(request),
            user_agent=str(request.headers.get("user-agent") or ""),
        )
        return {
            "item": _subscription_queue_item(saved, expose_checkout_url=True, operator_labels=_operator_label_map()),
            "stats": account_stats_db(),
            "marked": True,
            "verified": auto_verified,
            "auto_verified": auto_verified,
            "verification_pending": not auto_verified,
            "plan_type": plan_type,
            "verification_error": verification_message,
            "verification_message": verification_message,
        }

    @app.post("/api/operator/subscriptions/{account_id}/mark-failed")
    def api_operator_mark_failed(account_id: int, payload: SubscriptionActionPayload, request: Request) -> dict[str, Any]:
        user = _require_operator_permission(request, "mark_subscription_failed")
        try:
            saved = mark_subscription_account_failed_db(account_id, int(user["id"]), note=payload.note)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        record_audit_log(int(user["id"]), "mark_failed", target_type="account", target_id=str(account_id), detail={"note": payload.note}, ip=_request_ip(request), user_agent=str(request.headers.get("user-agent") or ""))
        return {"item": _subscription_queue_item(saved, expose_checkout_url=True, operator_labels=_operator_label_map()), "stats": account_stats_db()}

    @app.post("/api/operator/subscriptions/{account_id}/release")
    def api_operator_release(account_id: int, request: Request) -> dict[str, Any]:
        user = _require_operator_permission(request, "claim_subscription_account")
        try:
            saved = release_subscription_account_db(account_id, int(user["id"]))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        record_audit_log(int(user["id"]), "release_subscription", target_type="account", target_id=str(account_id), detail=None, ip=_request_ip(request), user_agent=str(request.headers.get("user-agent") or ""))
        return {"item": _subscription_queue_item(saved, expose_checkout_url=True, operator_labels=_operator_label_map()), "stats": account_stats_db()}

    @app.post("/api/admin/subscriptions/{account_id}/verify")
    def api_admin_verify_subscription(account_id: int, request: Request) -> dict[str, Any]:
        user = _require_admin_user(request)
        try:
            saved, plan_type = _refresh_and_verify_subscription_account(
                account_id,
                runtime,
                proxy=_next_proxy_for_runtime(runtime),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        record_audit_log(int(user["id"]), "verify_subscription", target_type="account", target_id=str(account_id), detail={"plan_type": plan_type}, ip=_request_ip(request), user_agent=str(request.headers.get("user-agent") or ""))
        return {"item": _account_detail(saved, _operator_label_map()), "stats": account_stats_db(), "verified": True, "plan_type": plan_type}

    @app.get("/api/accounts")
    def api_accounts(
        search: str = "",
        status: str = Query("all"),
        plan: str = Query("all"),
        stock_status: str = Query("all"),
        subscription_status: str = Query("all"),
        page: int = Query(1, ge=1),
        page_size: int = Query(50, ge=1, le=500),
    ) -> dict[str, Any]:
        operator_labels = _operator_label_map()
        total = count_account_rows(search=search, status=status, plan=plan, stock_status=stock_status, subscription_status=subscription_status)
        total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
        effective_page = min(max(1, page), total_pages)
        rows = list_account_rows(
            search=search,
            status=status,
            plan=plan,
            stock_status=stock_status,
            subscription_status=subscription_status,
            page=effective_page,
            page_size=page_size,
        )
        return {
            "items": [_account_summary(row, operator_labels) for row in rows],
            "stats": account_stats_db(),
            "pagination": {
                "page": effective_page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
            },
        }

    @app.patch("/api/accounts/batch/stock-status")
    def api_batch_update_stock_status(payload: BatchStockStatusPayload) -> dict[str, Any]:
        ids = _unique_positive_ids(payload.ids)
        operator_labels = _operator_label_map()
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for account_id in ids:
            try:
                saved = update_account_stock_status_db(account_id, payload.stock_status)
                results.append(_account_summary(saved, operator_labels))
            except Exception as exc:
                errors.append({"id": account_id, "error": str(exc)})
        return {"updated": len(results), "failed": errors, "items": results, "stats": account_stats_db()}

    @app.patch("/api/accounts/batch/subscription-type/refresh")
    def api_batch_refresh_subscription_type(payload: BatchIdsPayload) -> dict[str, Any]:
        ids = _unique_positive_ids(payload.ids)
        proxy_pool = _resolve_runtime_proxy_pool(runtime)
        operator_labels = _operator_label_map()
        results: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for account_id in ids:
            try:
                saved = _refresh_account_subscription_type(
                    account_id,
                    runtime,
                    proxy=pick_proxy_from_pool(proxy_pool),
                )
                results.append(_account_summary(saved, operator_labels))
            except Exception as exc:
                errors.append({"id": account_id, "error": str(exc)})
        return {"updated": len(results), "failed": errors, "items": results, "stats": account_stats_db()}

    @app.post("/api/accounts/batch/delete")
    def api_batch_delete_accounts(payload: BatchIdsPayload) -> dict[str, Any]:
        ids = _unique_positive_ids(payload.ids)
        deleted = 0
        errors: list[dict[str, Any]] = []
        for account_id in ids:
            try:
                if delete_account_db(account_id):
                    deleted += 1
                else:
                    errors.append({"id": account_id, "error": "账号不存在"})
            except Exception as exc:
                errors.append({"id": account_id, "error": str(exc)})
        return {"deleted": deleted, "failed": errors, "stats": account_stats_db()}

    @app.post("/api/accounts/batch/export-jsonl")
    def api_batch_export_accounts_jsonl(payload: BatchIdsPayload) -> Response:
        ids = _unique_positive_ids(payload.ids)
        accounts = list_account_rows_by_ids(ids)
        if not accounts:
            raise HTTPException(status_code=404, detail="没有找到可导出的账号")
        lines: list[str] = []
        skipped = 0
        for account in accounts:
            line = _session_jsonl_line(account.get("session"))
            if line is None:
                skipped += 1
                continue
            lines.append(line)
        if not lines:
            raise HTTPException(status_code=404, detail="所选账号没有可导出的 session")
        content = "\n".join(lines) + "\n"
        filename = f"sessions_selected_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-Count": str(len(lines)),
            "X-Skipped-Count": str(skipped),
        }
        return Response(content=content, media_type="application/x-ndjson; charset=utf-8", headers=headers)

    @app.post("/api/accounts/batch/export-airgate")
    def api_batch_export_accounts_airgate(payload: BatchIdsPayload) -> Response:
        ids = _unique_positive_ids(payload.ids)
        accounts = list_account_rows_by_ids(ids)
        if not accounts:
            raise HTTPException(status_code=404, detail="没有找到可导出的账号")
        content, exported_count, skipped = build_airgate_accounts_json(accounts)
        if not content:
            raise HTTPException(status_code=404, detail="所选账号没有可导出的 OpenAI 账号")
        filename = f"airgate-accounts_selected_{time.strftime('%Y%m%d%H%M%S')}.json"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-Count": str(exported_count),
            "X-Skipped-Count": str(skipped),
        }
        return Response(content=content, media_type="application/json; charset=utf-8", headers=headers)

    @app.get("/api/accounts/{account_id}")
    def api_account_detail(account_id: int) -> dict[str, Any]:
        account = get_account_db(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        return {"item": _account_detail(account, _operator_label_map())}

    @app.post("/api/accounts")
    def api_create_account(payload: AccountPayload) -> dict[str, Any]:
        data = _payload_data(payload)
        try:
            saved = save_account_db_record(data)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail="邮箱已存在，不能保存重复账号") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"item": _account_detail(saved, _operator_label_map()), "stats": account_stats_db()}

    @app.put("/api/accounts/{account_id}")
    def api_update_account(account_id: int, payload: AccountPayload) -> dict[str, Any]:
        data = _payload_data(payload)
        try:
            saved = save_account_db_record(data, account_id=account_id)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail="邮箱已存在，不能保存重复账号") from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"item": _account_detail(saved, _operator_label_map()), "stats": account_stats_db()}

    @app.patch("/api/accounts/{account_id}/stock-status")
    def api_update_stock_status(account_id: int, payload: StockStatusPayload) -> dict[str, Any]:
        try:
            saved = update_account_stock_status_db(account_id, payload.stock_status)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"item": _account_detail(saved, _operator_label_map()), "stats": account_stats_db()}

    @app.patch("/api/accounts/{account_id}/subscription-type/refresh")
    def api_refresh_subscription_type(account_id: int) -> dict[str, Any]:
        try:
            saved = _refresh_account_subscription_type(
                account_id,
                runtime,
                proxy=_next_proxy_for_runtime(runtime),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"item": _account_detail(saved, _operator_label_map()), "stats": account_stats_db()}

    @app.patch("/api/accounts/{account_id}/subscription-type/mark-subscribed")
    def api_mark_account_subscribed(account_id: int) -> dict[str, Any]:
        try:
            saved, plan_type = _refresh_and_verify_subscription_account(
                account_id,
                runtime,
                proxy=_next_proxy_for_runtime(runtime),
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"item": _account_detail(saved, _operator_label_map()), "stats": account_stats_db(), "marked": True, "verified": True, "plan_type": plan_type}

    @app.patch("/api/accounts/{account_id}/status/abandon")
    def api_mark_account_abandoned(account_id: int) -> dict[str, Any]:
        account = get_account_db(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        try:
            saved = update_account_status_db(account_id, ACCOUNT_STATUS_ABANDONED)
            if saved.get("stock_status") == STOCK_STATUS_OUT:
                saved = update_account_stock_status_db(account_id, STOCK_STATUS_IN)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"item": _account_detail(saved, _operator_label_map()), "stats": account_stats_db(), "marked": True}

    @app.delete("/api/accounts/{account_id}")
    def api_delete_account(account_id: int) -> dict[str, Any]:
        deleted = delete_account_db(account_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="账号不存在")
        return {"deleted": True, "stats": account_stats_db()}

    @app.post("/api/export")
    def api_export() -> Response:
        content = build_accounts_db_txt()
        if not content:
            raise HTTPException(status_code=404, detail="没有可导出的账号")
        filename = f"accounts_{time.strftime('%Y%m%d_%H%M%S')}.txt"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-Count": str(len([line for line in content.splitlines() if line.strip()])),
        }
        return Response(content=content, media_type="text/plain; charset=utf-8", headers=headers)

    @app.post("/api/export/tokens")
    def api_export_tokens() -> Response:
        content = build_tokens_db_jsonl()
        if not content:
            raise HTTPException(status_code=404, detail="没有可导出的 token")
        filename = f"tokens_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-Count": str(len([line for line in content.splitlines() if line.strip()])),
        }
        return Response(content=content, media_type="application/x-ndjson; charset=utf-8", headers=headers)

    @app.post("/api/export/checkouts")
    def api_export_checkouts() -> Response:
        content = build_checkout_db_jsonl()
        if not content:
            raise HTTPException(status_code=404, detail="没有可导出的 checkout")
        filename = f"checkout_urls_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-Count": str(len([line for line in content.splitlines() if line.strip()])),
        }
        return Response(content=content, media_type="application/x-ndjson; charset=utf-8", headers=headers)

    @app.post("/api/export/airgate")
    def api_export_airgate() -> Response:
        content, exported_count, skipped = build_airgate_accounts_json()
        if not content:
            raise HTTPException(status_code=404, detail="没有可导出的 OpenAI 账号")
        filename = f"airgate-accounts_{time.strftime('%Y%m%d%H%M%S')}.json"
        headers = {
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Exported-Count": str(exported_count),
            "X-Skipped-Count": str(skipped),
        }
        return Response(content=content, media_type="application/json; charset=utf-8", headers=headers)

    @app.post("/api/ops/jobs")
    def api_start_job(payload: OperationPayload) -> dict[str, Any]:
        try:
            job = manager.start(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job": _job_view(job), "queue": manager.stats(), "auto": auto_scheduler.status()}

    @app.post("/api/ops/jobs/batch")
    def api_start_jobs(payload: BatchOperationPayload) -> dict[str, Any]:
        count = max(1, min(20, int(payload.count or 1)))
        mode = payload.mode.strip().lower()
        if count > 1 and mode != "register":
            raise HTTPException(status_code=400, detail="批量执行只支持 register 模式")
        if count > 1 and (
            payload.email.strip()
            or payload.password.strip()
            or not payload.generate_email
            or not payload.generate_password
        ):
            raise HTTPException(status_code=400, detail="批量注册必须使用随机邮箱和随机密码")
        jobs: list[dict[str, Any]] = []
        try:
            for job in manager.start_many(payload, count):
                jobs.append(_job_view(job))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"jobs": jobs, "queue": manager.stats(), "auto": auto_scheduler.status()}

    @app.get("/api/ops/jobs")
    def api_jobs() -> dict[str, Any]:
        return {
            "items": [_job_view(job) for job in manager.list_recent()],
            "queue": manager.stats(),
            "auto": auto_scheduler.status(),
            "airgate": airgate_monitor.status(),
        }

    @app.get("/api/ops/jobs/{job_id}")
    def api_job(job_id: str) -> dict[str, Any]:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"job": _job_view(job), "queue": manager.stats(), "auto": auto_scheduler.status(), "airgate": airgate_monitor.status()}

    @app.post("/api/ops/jobs/{job_id}/input")
    def api_job_input(job_id: str, payload: PromptPayload) -> dict[str, Any]:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job.status != "waiting":
            raise HTTPException(status_code=400, detail="任务当前不在等待输入状态")
        job.submit_input(payload.value)
        return {"job": _job_view(job), "queue": manager.stats(), "auto": auto_scheduler.status(), "airgate": airgate_monitor.status()}

    @app.get("/api/ops/auto-register")
    def api_auto_register_status() -> dict[str, Any]:
        return {"auto": auto_scheduler.status(), "queue": manager.stats(), "airgate": airgate_monitor.status()}

    @app.post("/api/ops/auto-register/start")
    def api_auto_register_start(payload: AutoRegisterPayload) -> dict[str, Any]:
        return {"auto": auto_scheduler.start(payload), "queue": manager.stats(), "airgate": airgate_monitor.status()}

    @app.post("/api/ops/auto-register/config")
    def api_auto_register_config(payload: AutoRegisterPayload) -> dict[str, Any]:
        return {"auto": auto_scheduler.configure(payload), "queue": manager.stats(), "airgate": airgate_monitor.status()}

    @app.post("/api/ops/auto-register/stop")
    def api_auto_register_stop() -> dict[str, Any]:
        return {"auto": auto_scheduler.stop(), "queue": manager.stats(), "airgate": airgate_monitor.status()}

    @app.get("/api/ops/airgate-monitor")
    def api_airgate_monitor_status() -> dict[str, Any]:
        return {"airgate": airgate_monitor.status()}

    @app.post("/api/ops/airgate-monitor/start")
    def api_airgate_monitor_start(payload: AirGateMonitorPayload) -> dict[str, Any]:
        airgate = airgate_monitor.start(_airgate_monitor_config_from_payload(payload, enabled=True))
        save_airgate_monitor_config(runtime.config_path, airgate_monitor.current_config())
        return {"airgate": airgate, "queue": manager.stats()}

    @app.post("/api/ops/airgate-monitor/config")
    def api_airgate_monitor_config(payload: AirGateMonitorPayload) -> dict[str, Any]:
        current_enabled = bool(airgate_monitor.status().get("enabled"))
        airgate = airgate_monitor.configure(_airgate_monitor_config_from_payload(payload, enabled=current_enabled))
        save_airgate_monitor_config(runtime.config_path, airgate_monitor.current_config())
        return {"airgate": airgate, "queue": manager.stats()}

    @app.post("/api/ops/airgate-monitor/stop")
    def api_airgate_monitor_stop() -> dict[str, Any]:
        airgate = airgate_monitor.stop()
        current = airgate_monitor.current_config()
        save_airgate_monitor_config(
            runtime.config_path,
            AirGateMonitorConfig(
                enabled=False,
                core_url=current.core_url,
                admin_key=current.admin_key,
                proxy=current.proxy,
                poll_interval_seconds=current.poll_interval_seconds,
                account_cooldown_seconds=current.account_cooldown_seconds,
                page_size=current.page_size,
            ),
        )
        return {"airgate": airgate, "queue": manager.stats()}

    @app.post("/api/ops/airgate-monitor/run-once")
    def api_airgate_monitor_run_once() -> dict[str, Any]:
        return {"airgate": airgate_monitor.run_once(), "queue": manager.stats()}

    @app.get("/api/ops/subscription-verify")
    def api_subscription_verify_status() -> dict[str, Any]:
        return {"verification": subscription_verify_scheduler.status(), "stats": account_stats_db()}

    return app


def _payload_data(payload: AccountPayload) -> dict[str, str]:
    data = payload.model_dump()
    session_text = str(data.get("session_json") or "").strip()
    if not session_text or session_text.lower() == NULL_VALUE:
        data["session_json"] = NULL_VALUE
        return data
    try:
        json.loads(session_text)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"session_json 不是有效 JSON: {exc.msg}") from exc
    data["session_json"] = session_text
    return data


def _render_html_page(page: str) -> str:
    requested = str(page).strip().lower()
    mode = requested if requested in {"accounts", "tasks", "settings"} else "accounts"
    titles = {
        "accounts": "Protocol Reg 账号管理",
        "tasks": "Protocol Reg 任务控制台",
        "settings": "Protocol Reg 设置",
    }
    html = HTML_PAGE.replace("<body>", f'<body data-page="{mode}">', 1)
    if mode != "accounts":
        html = html.replace("<title>Protocol Reg 账号管理</title>", f"<title>{titles[mode]}</title>", 1)
    return html


def _render_login_page() -> str:
    return LOGIN_PAGE


def _render_admin_users_page() -> str:
    return ADMIN_USERS_PAGE


def _render_operator_page() -> str:
    return OPERATOR_PAGE


def _public_web_user(user: dict[str, Any]) -> dict[str, Any]:
    permissions = user.get("permissions")
    if not isinstance(permissions, list):
        permissions = []
    return {
        "id": str(user.get("id") or ""),
        "username": str(user.get("username") or "").strip().lower(),
        "display_name": str(user.get("display_name") or "").strip(),
        "role": str(user.get("role") or AUTH_ROLE_OPERATOR).strip().lower(),
        "permissions": [str(item) for item in permissions if str(item or "").strip()],
        "status": str(user.get("status") or AUTH_STATUS_ACTIVE).strip().lower(),
        "must_change_password": str(user.get("must_change_password") or "false").lower() in {"1", "true", "yes"},
        "created_at": str(user.get("created_at") or ""),
        "updated_at": str(user.get("updated_at") or ""),
        "last_login_at": str(user.get("last_login_at") or ""),
        "last_login_ip": str(user.get("last_login_ip") or ""),
        "last_login_user_agent": str(user.get("last_login_user_agent") or ""),
    }


def _operator_label_map() -> dict[str, str]:
    labels: dict[str, str] = {}
    for user in list_web_users():
        user_id = str(user.get("id") or "").strip()
        if not user_id:
            continue
        display_name = str(user.get("display_name") or "").strip()
        username = str(user.get("username") or "").strip()
        labels[user_id] = display_name or username or f"#{user_id}"
    return labels


def _operator_label(operator_id: object, labels: dict[str, str] | None = None) -> str:
    text = str(operator_id or "").strip()
    if not text or text.lower() == NULL_VALUE:
        return "未领取"
    label_map = labels or {}
    return label_map.get(text, f"#{text}")


def _is_paid_subscription_plan(plan_type: object) -> bool:
    normalized = str(plan_type or "").strip().lower()
    return bool(normalized and normalized not in {"null", "none", "free"})


def _subscription_queue_item(
    account: dict[str, Any],
    *,
    expose_checkout_url: bool = True,
    operator_labels: dict[str, str] | None = None,
) -> dict[str, Any]:
    status = str(account.get("subscription_status") or SUBSCRIPTION_STATUS_PENDING)
    operator_id = str(account.get("subscription_operator_id") or "")
    checkout_url = str(account.get("checkout_url") or NULL_VALUE)
    if not expose_checkout_url:
        checkout_url = NULL_VALUE
    return {
        "id": str(account.get("id") or ""),
        "email": str(account.get("email") or ""),
        "subscription_type": str(account.get("subscription_type") or NULL_VALUE),
        "checkout_url": checkout_url,
        "subscription_status": status,
        "subscription_operator_id": operator_id,
        "subscription_operator_name": _operator_label(operator_id, operator_labels),
        "subscription_claimed_at": str(account.get("subscription_claimed_at") or NULL_VALUE),
        "subscription_claim_expires_at": str(account.get("subscription_claim_expires_at") or NULL_VALUE),
        "subscription_marked_at": str(account.get("subscription_marked_at") or NULL_VALUE),
        "subscription_verified_at": str(account.get("subscription_verified_at") or NULL_VALUE),
        "subscription_verify_attempts": int(account.get("subscription_verify_attempts") or 0),
        "subscription_verify_last_at": str(account.get("subscription_verify_last_at") or NULL_VALUE),
        "subscription_verify_next_at": str(account.get("subscription_verify_next_at") or NULL_VALUE),
        "subscription_verify_last_message": str(account.get("subscription_verify_last_message") or NULL_VALUE),
        "subscription_note": str(account.get("subscription_note") or NULL_VALUE),
        "stock_status": str(account.get("stock_status") or STOCK_STATUS_IN),
        "created_at": str(account.get("created_at") or NULL_VALUE),
        "updated_at": str(account.get("updated_at") or NULL_VALUE),
        "last_login_at": str(account.get("last_login_at") or NULL_VALUE),
        "is_claimed": status == SUBSCRIPTION_STATUS_CLAIMED,
        "is_verified": status == SUBSCRIPTION_STATUS_VERIFIED,
        "is_failed": status == SUBSCRIPTION_STATUS_FAILED,
    }


def _request_ip(request: Request) -> str:
    forwarded = str(request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        return forwarded
    client = request.client
    return str(getattr(client, "host", "") or "")


def _unique_positive_ids(ids: list[int]) -> list[int]:
    unique: list[int] = []
    seen: set[int] = set()
    for raw_id in ids:
        account_id = int(raw_id)
        if account_id <= 0 or account_id in seen:
            continue
        seen.add(account_id)
        unique.append(account_id)
    if not unique:
        raise HTTPException(status_code=400, detail="请先选择账号")
    return unique


def _next_proxy_for_runtime(runtime: WebRuntime) -> str:
    return pick_proxy_from_pool(_resolve_runtime_proxy_pool(runtime))


def _refresh_account_subscription_type(
    account_id: int,
    runtime: WebRuntime,
    *,
    proxy: str | None = None,
) -> dict[str, str]:
    account = get_account_db(account_id)
    if account is None:
        raise KeyError(f"账号不存在: {account_id}")
    access_token = _session_access_token(account.get("session"))
    if not access_token:
        raise ValueError("当前账号没有保存 session accessToken")
    plan_type = _fetch_subscription_type_by_access_token(access_token, runtime, proxy=proxy)
    return update_account_subscription_type_db(account_id, plan_type)


def _refresh_and_verify_subscription_account(
    account_id: int,
    runtime: WebRuntime,
    *,
    proxy: str | None = None,
) -> tuple[dict[str, str], str]:
    refreshed = _refresh_account_subscription_type(account_id, runtime, proxy=proxy)
    plan_type = str(refreshed.get("subscription_type") or NULL_VALUE).strip() or NULL_VALUE
    if not _is_paid_subscription_plan(plan_type):
        raise ValueError(f"核实未通过，当前订阅类型为 {plan_type}")
    verified = verify_subscription_account_db(account_id)
    return verified, plan_type


def _try_auto_verify_subscription_account(
    account_id: int,
    runtime: WebRuntime,
    *,
    proxy: str | None = None,
) -> tuple[dict[str, str], str, str | None]:
    refreshed = _refresh_account_subscription_type(account_id, runtime, proxy=proxy)
    plan_type = str(refreshed.get("subscription_type") or NULL_VALUE).strip() or NULL_VALUE
    if not _is_paid_subscription_plan(plan_type):
        return refreshed, plan_type, f"当前订阅类型为 {plan_type}"
    verified = verify_subscription_account_db(account_id)
    return verified, plan_type, None


def _utc_after_seconds(seconds: int) -> str:
    delay = max(0, int(seconds))
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()


def _subscription_verify_delay_seconds(attempts: int) -> int:
    attempt = max(1, int(attempts))
    delay = 3 * (2 ** (attempt - 1))
    return min(120, delay)


def _has_value(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text and text.lower() != NULL_VALUE)


def _secret_preview(value: object) -> str:
    text = str(value or "").strip()
    if not _has_value(text):
        return "未保存"
    if len(text) <= 10:
        return f"已保存 · {len(text)} 字符"
    return f"{text[:4]}…{text[-4:]} · {len(text)} 字符"


def _account_summary(account: dict[str, str], operator_labels: dict[str, str] | None = None) -> dict[str, Any]:
    operator_id = account.get("subscription_operator_id", NULL_VALUE)
    return {
        "id": int(account.get("id") or 0),
        "email": account.get("email", ""),
        "subscription_type": account.get("subscription_type", NULL_VALUE),
        "subscription_status": account.get("subscription_status", SUBSCRIPTION_STATUS_PENDING),
        "subscription_operator_id": operator_id,
        "subscription_operator_name": _operator_label(operator_id, operator_labels),
        "subscription_claimed_at": account.get("subscription_claimed_at", NULL_VALUE),
        "subscription_claim_expires_at": account.get("subscription_claim_expires_at", NULL_VALUE),
        "subscription_marked_at": account.get("subscription_marked_at", NULL_VALUE),
        "subscription_verified_at": account.get("subscription_verified_at", NULL_VALUE),
        "subscription_verify_attempts": int(account.get("subscription_verify_attempts") or 0),
        "subscription_verify_last_at": account.get("subscription_verify_last_at", NULL_VALUE),
        "subscription_verify_next_at": account.get("subscription_verify_next_at", NULL_VALUE),
        "subscription_verify_last_message": account.get("subscription_verify_last_message", NULL_VALUE),
        "subscription_note": account.get("subscription_note", NULL_VALUE),
        "status": account.get("status", NULL_VALUE),
        "stock_status": account.get("stock_status", STOCK_STATUS_IN),
        "created_at": account.get("created_at", NULL_VALUE),
        "updated_at": account.get("updated_at", NULL_VALUE),
        "last_login_at": account.get("last_login_at", NULL_VALUE),
        "last_authorized_at": account.get("last_authorized_at", NULL_VALUE),
        "has_password": _has_value(account.get("password")),
        "has_refresh_token": _has_value(account.get("refresh_token")),
        "has_session": _has_value(account.get("session")),
        "has_checkout_url": _has_value(account.get("checkout_url")),
        "has_login_session": _has_value(account.get("login_session")),
        "has_auth_token": _has_value(account.get("auth_token")),
        "password_preview": _secret_preview(account.get("password")),
        "refresh_token_preview": _secret_preview(account.get("refresh_token")),
        "session_preview": _secret_preview(account.get("session")),
        "checkout_url_preview": _secret_preview(account.get("checkout_url")),
    }


def _account_detail(account: dict[str, str], operator_labels: dict[str, str] | None = None) -> dict[str, Any]:
    detail = _account_summary(account, operator_labels)
    detail.update(
        {
            "password": account.get("password", NULL_VALUE),
            "refresh_token": account.get("refresh_token", NULL_VALUE),
            "session_json": account.get("session", NULL_VALUE),
            "checkout_url": account.get("checkout_url", NULL_VALUE),
            "subscription_status": account.get("subscription_status", SUBSCRIPTION_STATUS_PENDING),
            "subscription_operator_id": account.get("subscription_operator_id", NULL_VALUE),
            "subscription_claimed_at": account.get("subscription_claimed_at", NULL_VALUE),
            "subscription_claim_expires_at": account.get("subscription_claim_expires_at", NULL_VALUE),
            "subscription_marked_at": account.get("subscription_marked_at", NULL_VALUE),
            "subscription_verified_at": account.get("subscription_verified_at", NULL_VALUE),
            "subscription_verify_attempts": int(account.get("subscription_verify_attempts") or 0),
            "subscription_verify_last_at": account.get("subscription_verify_last_at", NULL_VALUE),
            "subscription_verify_next_at": account.get("subscription_verify_next_at", NULL_VALUE),
            "subscription_verify_last_message": account.get("subscription_verify_last_message", NULL_VALUE),
            "subscription_note": account.get("subscription_note", NULL_VALUE),
            "login_session_json": account.get("login_session", NULL_VALUE),
            "auth_token_json": account.get("auth_token", NULL_VALUE),
            "checkout_json": account.get("checkout", NULL_VALUE),
            "created_at": account.get("created_at", NULL_VALUE),
        }
    )
    return detail


def _session_jsonl_line(session_text: object) -> str | None:
    text = str(session_text or "").strip()
    if not _has_value(text):
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, (dict, list)):
        return None
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _job_view(job: WebJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "mode": job.mode,
        "email": job.email,
        "proxy": proxy_preview(job.proxy),
        "status": job.status,
        "queue_position": job.queue_position,
        "prompt": job.prompt,
        "error": job.error,
        "result": job.result,
        "logs": job.logs[-300:],
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _resolve_runtime_proxy_pool(runtime: WebRuntime, cfg: AppConfig | None = None) -> tuple[str, ...]:
    cfg = cfg or load_app_config(runtime.config_path)
    return resolve_proxy_pool(
        runtime.proxy,
        os.environ.get("PROTOCOL_REG_PROXIES", ""),
        os.environ.get("PROTOCOL_REG_PROXY", ""),
        cfg.proxies,
        cfg.proxy,
    )


def _settings_from_runtime(runtime: WebRuntime, *, proxy: str | None = None) -> Settings:
    cfg = load_app_config(runtime.config_path)
    if proxy is None:
        pool = _resolve_runtime_proxy_pool(runtime, cfg)
        proxy = pool[0] if pool else ""
    email_code_api = os.environ.get("EMAIL_CODE_API", "").strip() or cfg.email_code_api
    email_code_key = os.environ.get("EMAIL_CODE_API_KEY", "").strip() or cfg.email_code_key
    email_code_sender_suffix = (
        os.environ.get("EMAIL_CODE_SENDER_SUFFIX", "").strip()
        or cfg.email_code_sender_suffix
        or "openai.com"
    )
    email_code_timeout = int(os.environ.get("EMAIL_CODE_TIMEOUT", "0") or 0) or cfg.email_code_timeout
    email_code_poll = float(os.environ.get("EMAIL_CODE_POLL", "0") or 0) or cfg.email_code_poll
    otp_max_retries = _positive_int(os.environ.get("EMAIL_CODE_MAX_OTP_RETRIES"), cfg.otp_max_retries)
    otp_poll_max_attempts = _positive_int(
        os.environ.get("EMAIL_CODE_OTP_POLL_MAX_ATTEMPTS"),
        cfg.otp_poll_max_attempts,
    )
    use_proxy_for_email = _boolish(os.environ.get("EMAIL_CODE_USE_PROXY"), cfg.use_proxy_for_email)
    return Settings(
        project_root=runtime.repo_root.resolve(),
        proxy=proxy,
        license_file=runtime.license_file,
        login_delay=max(0, runtime.login_delay),
        timeout=max(1, runtime.timeout),
        ssl_verify=runtime.ssl_verify,
        email_code_api_base=str(email_code_api or "").strip(),
        email_code_api_key=str(email_code_key or "").strip(),
        email_code_sender_suffix=str(email_code_sender_suffix or "openai.com").strip() or "openai.com",
        email_code_poll_interval=max(0.5, float(email_code_poll)),
        email_code_timeout=max(5, int(email_code_timeout)),
        otp_max_retries=max(1, int(otp_max_retries)),
        otp_poll_max_attempts=max(1, int(otp_poll_max_attempts)),
        use_proxy_for_email=bool(use_proxy_for_email),
    )


def _airgate_monitor_proxy(monitor: AirGate401Monitor) -> str:
    try:
        config = monitor.current_config()
    except Exception:
        return ""
    return str(config.proxy or "").strip()


def _airgate_monitor_config_from_payload(payload: AirGateMonitorPayload, *, enabled: bool) -> AirGateMonitorConfig:
    return AirGateMonitorConfig(
        enabled=enabled,
        core_url=str(payload.core_url or "").strip(),
        admin_key=str(payload.admin_key or "").strip(),
        proxy=str(payload.proxy or "").strip(),
        poll_interval_seconds=max(10, int(payload.poll_interval_seconds or 10)),
        account_cooldown_seconds=max(60, int(payload.account_cooldown_seconds or 60)),
        page_size=min(100, max(1, int(payload.page_size or 100))),
    )


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _boolish(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_operation_account(
    payload: OperationPayload,
    cfg: AppConfig,
    runtime: WebRuntime,
    *,
    reserved_emails: set[str] | None = None,
) -> tuple[str, str]:
    account = get_account_db(payload.account_id) if payload.account_id else None
    if payload.account_id and account is None:
        raise ValueError(f"账号不存在: {payload.account_id}")
    email = str(payload.email or "").strip().lower()
    password = str(payload.password or "")
    reserved = {item.strip().lower() for item in (reserved_emails or set()) if item.strip()}
    if account is not None:
        email = email or account.get("email", "")
        password = password or account.get("password", "")

    if payload.mode == "register" and not email and payload.generate_email:
        email = _random_email(cfg.email_suffixes, reserved)
    if payload.mode == "register" and not password and payload.generate_password:
        password = make_password()

    if not email:
        raise ValueError("邮箱不能为空")
    if payload.mode == "register" and email in reserved:
        raise ValueError(f"邮箱已在当前任务队列中，避免重复注册: {email}")
    if payload.mode == "register" and _email_exists(email):
        raise ValueError(f"邮箱已存在于数据库，避免重复注册: {email}")
    if payload.mode in {"register", "login"} and not password:
        raise ValueError("密码不能为空")
    if payload.mode == "authorize" and not password:
        session_data = try_load_login_session_db(email)
        if session_data is None:
            raise ValueError("授权需要已有登录会话或账号密码")
    return email, password


def _chatgpt_session_payload(session_data: dict[str, Any]) -> dict[str, Any] | None:
    chatgpt_session = session_data.get("chatgpt_session")
    if not isinstance(chatgpt_session, dict):
        return None
    data = chatgpt_session.get("data")
    if isinstance(data, dict):
        return data
    if "accessToken" in chatgpt_session or "user" in chatgpt_session:
        return chatgpt_session
    return None


def _require_chatgpt_session_payload(session_data: dict[str, Any]) -> dict[str, Any]:
    payload = _chatgpt_session_payload(session_data)
    if isinstance(payload, dict) and _session_access_token(payload):
        return payload
    chatgpt_session = session_data.get("chatgpt_session")
    if isinstance(chatgpt_session, dict):
        status_code = chatgpt_session.get("status_code")
        error = str(chatgpt_session.get("error") or "").strip()
        if error:
            raise RuntimeError(f"登录完成但未获取到有效 ChatGPT Session: {error}")
        if status_code:
            raise RuntimeError(f"登录完成但 ChatGPT Session 接口返回 HTTP {status_code}")
    raise RuntimeError("登录完成但未获取到有效 ChatGPT Session accessToken")


def _execute_operation(
    job: WebJob,
    payload: OperationPayload,
    settings: Settings,
    flow: RegisterFlow,
    email: str,
    password: str,
    runtime: WebRuntime,
) -> dict[str, Any]:
    mode = payload.mode.strip().lower()
    if mode == "register":
        token_data = flow.run(email, password)
        save_account_storage(email, password, token_data, source="register")
        checkout = None
        if payload.create_checkout:
            checkout = flow.create_plus_trial_checkout(
                flow.http.session,
                token_data.get("chatgpt_session") if isinstance(token_data.get("chatgpt_session"), dict) else {},
            )
        checkout_url = _store_checkout_for_account(email, checkout, runtime)
        return {"email": token_data.get("email") or email, "checkout_url": checkout_url}

    if mode == "login":
        session_data = flow.login(email, password, create_checkout=payload.create_checkout)
        session_payload = _require_chatgpt_session_payload(session_data)
        save_login_session_db(email, password, session_data)
        checkout = session_data.get("plus_trial_checkout") if payload.create_checkout else None
        checkout_url = _store_checkout_for_account(email, checkout, runtime)
        plan_type = _extract_session_subscription_type(session_payload)
        return {
            "email": email,
            "checkout_url": checkout_url,
            "session_saved": True,
            "subscription_type": plan_type or NULL_VALUE,
        }

    session_data = try_load_login_session_db(email)
    if session_data is None:
        job.log("[授权] 未找到可用登录会话，使用账号密码即时登录")
        session_data = flow.login(email, password, create_checkout=False)
        save_login_session_db(email, password, session_data)
    else:
        password = str(session_data.get("password") or password)
    token_data = flow.authorize_from_session(email, session_data)
    save_authorization_token_db(email, password, token_data)
    return {"email": token_data.get("email") or email, "token_saved": True}


def _email_exists(email: str) -> bool:
    expected = email.strip().lower()
    return any(str(account.get("email") or "").strip().lower() == expected for account in list_account_rows())


def _random_email(suffixes: tuple[str, ...], reserved_emails: set[str] | None = None) -> str:
    existing = {account.get("email", "") for account in list_account_rows()}
    reserved = {item.strip().lower() for item in (reserved_emails or set()) if item.strip()}
    return make_random_email(suffixes, existing | reserved)


def _checkout_long_url(checkout: object) -> str:
    if not isinstance(checkout, dict):
        return ""
    for key in ("long_url", "hosted_url", "openai_payurl"):
        url = str(checkout.get(key) or "").strip()
        if url:
            return url
    return ""


def _store_checkout_for_account(email: str, checkout: object, runtime: WebRuntime) -> str:
    checkout_url = _checkout_long_url(checkout)
    if not checkout_url:
        return ""
    update_account_checkout_url_db(email, checkout_url, checkout if isinstance(checkout, dict) else None)
    return checkout_url


def _session_access_token(session_json: object) -> str:
    if isinstance(session_json, dict):
        session = session_json
    else:
        text = str(session_json or "").strip()
        if not text or text.lower() == NULL_VALUE:
            return ""
        try:
            session = json.loads(text)
        except json.JSONDecodeError:
            return ""
    if isinstance(session, dict) and isinstance(session.get("data"), dict):
        session = session["data"]
    if not isinstance(session, dict):
        return ""
    return str(session.get("accessToken") or session.get("access_token") or "").strip()


def _extract_session_subscription_type(session_json: object) -> str:
    if isinstance(session_json, dict):
        session = session_json
    else:
        text = str(session_json or "").strip()
        if not text or text.lower() == NULL_VALUE:
            return ""
        try:
            session = json.loads(text)
        except json.JSONDecodeError:
            return ""
    if isinstance(session, dict) and isinstance(session.get("data"), dict):
        session = session["data"]
    if not isinstance(session, dict):
        return ""
    for key in ("subscription_type", "subscriptionType", "planType", "plan_type"):
        value = str(session.get(key) or "").strip()
        if value:
            return value
    account = session.get("account")
    if isinstance(account, dict):
        for key in ("planType", "plan_type", "subscription_type", "subscriptionType"):
            value = str(account.get(key) or "").strip()
            if value:
                return value
    return ""


def _fetch_subscription_type_by_access_token(
    access_token: str,
    runtime: WebRuntime,
    *,
    proxy: str | None = None,
) -> str:
    try:
        settings = _settings_from_runtime(runtime, proxy=proxy)
        response = requests.get(
            "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
            headers=_accounts_check_headers(access_token),
            proxies=settings.proxies,
            verify=settings.ssl_verify,
            timeout=max(1, settings.timeout),
            impersonate="chrome136",
        )
    except Exception as exc:
        raise RuntimeError(f"账号类型请求失败: {exc}") from exc
    if response.status_code < 200 or response.status_code >= 300:
        raise RuntimeError(f"账号类型请求失败 HTTP {response.status_code}: {response.text[:300]}")
    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError("账号类型响应不是有效 JSON") from exc
    plan_type = _select_subscription_type(data)
    if not plan_type:
        raise ValueError("账号类型响应里没有 plan_type 或 subscription_plan")
    return plan_type


def _accounts_check_headers(access_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Authorization": f"Bearer {access_token}",
        "Cache-Control": "no-cache",
        "Origin": "https://chatgpt.com",
        "Pragma": "no-cache",
        "Referer": "https://chatgpt.com/",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
    }


def _select_subscription_type(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    accounts = data.get("accounts")
    if not isinstance(accounts, dict):
        return ""
    default_plan = ""
    paid_plan = ""
    any_plan = ""
    for raw in accounts.values():
        if not isinstance(raw, dict):
            continue
        plan_type = _account_check_plan_type(raw)
        if not plan_type:
            continue
        if not any_plan:
            any_plan = plan_type
        if plan_type.lower() != "free" and not paid_plan:
            paid_plan = plan_type
        account_data = raw.get("account")
        if isinstance(account_data, dict) and account_data.get("is_default") is True:
            default_plan = plan_type
    return default_plan or paid_plan or any_plan


def _account_check_plan_type(account: dict[str, Any]) -> str:
    account_data = account.get("account")
    if isinstance(account_data, dict):
        plan_type = str(account_data.get("plan_type") or "").strip()
        if plan_type:
            return plan_type
    entitlement = account.get("entitlement")
    if isinstance(entitlement, dict):
        plan_type = str(entitlement.get("subscription_plan") or "").strip()
        if plan_type:
            return plan_type
    return ""


def _default_license_file(repo_root: Path) -> Path | None:
    for candidate in (repo_root / "config" / "wenfxl.license", repo_root / "wenfxl.license"):
        if candidate.exists():
            return candidate.resolve()
    return None


def build_parser() -> argparse.ArgumentParser:
    repo_root = _repo_root()
    parser = argparse.ArgumentParser(description="Protocol Reg 本地账号管理 Web 页面")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认 0.0.0.0，允许局域网访问")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--db", default=str(repo_root / "data" / "data.db"), help="SQLite 数据库路径")
    parser.add_argument("--config", default=str(repo_root / "config" / "protocol-reg.yaml"), help="配置文件路径")
    parser.add_argument("--proxy", default="", help="注册/登录/授权代理；多个代理可用逗号分隔")
    parser.add_argument("--max-concurrency", type=int, default=0, help="任务最大并发数，0 表示读取配置文件")
    parser.add_argument("--license-file", default="", help="auth_core 授权文件路径")
    parser.add_argument("--login-delay", type=int, default=20, help="注册成功后等待多少秒再获取 ChatGPT session")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP 超时时间，单位秒")
    parser.add_argument("--no-ssl-verify", action="store_true", help="关闭 TLS 证书校验")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    db_path = Path(args.db).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    os.environ["PROTOCOL_REG_DB_PATH"] = str(db_path)

    repo_root = _repo_root()
    license_file = Path(args.license_file).resolve() if args.license_file else _default_license_file(repo_root)
    app_cfg = load_app_config(Path(args.config).resolve())
    max_concurrency = int(args.max_concurrency) if int(args.max_concurrency) > 0 else app_cfg.max_concurrency
    runtime = WebRuntime(
        repo_root=repo_root,
        config_path=Path(args.config).resolve(),
        license_file=license_file,
        proxy=str(args.proxy or "").strip(),
        login_delay=max(0, args.login_delay),
        timeout=max(1, args.timeout),
        ssl_verify=not args.no_ssl_verify,
        max_concurrency=max(1, max_concurrency),
    )
    app = create_app(runtime=runtime)
    bootstrap_admin = getattr(app.state, "bootstrap_admin", None)
    urls = _display_urls(args.host, args.port)
    print("[Web] 账号管理页面:")
    for url in urls:
        print(f"  {url}")
    print(f"[Web] 数据库: {db_path}")
    print(f"[Web] 配置文件: {runtime.config_path}")
    print("[Web] 数据存储: SQLite DB-only；TXT/JSON 仅按需下载导出")
    print(f"[Web] 任务最大并发: {runtime.max_concurrency}")
    if isinstance(bootstrap_admin, dict):
        print(f"[Web] 初始管理员: {bootstrap_admin.get('username')}")
        print(f"[Web] 初始管理员密码: {bootstrap_admin.get('password')}")
    print("[Web] 页面路径: / 账号管理 · /tasks 任务控制台 · /settings 自动注册设置")
    proxy_pool = _resolve_runtime_proxy_pool(runtime, app_cfg)
    if len(proxy_pool) > 1:
        print(f"[Web] 代理池: {len(proxy_pool)} 个，任务按轮询分配")
    elif proxy_pool:
        print(f"[Web] 代理: {proxy_preview(proxy_pool[0])}")
    if app_cfg.airgate_monitor.core_url.strip() and app_cfg.airgate_monitor.admin_key.strip():
        print(f"[Web] AirGate 401 监控: {app_cfg.airgate_monitor.core_url.strip()}，已启用配置")
    if args.open:
        webbrowser.open(urls[0])
    uvicorn.run(app, host=args.host, port=args.port)


def _display_urls(host: str, port: int) -> list[str]:
    host = str(host or "").strip() or "0.0.0.0"
    if host not in {"0.0.0.0", "::"}:
        return [f"http://{host}:{port}"]

    urls = [f"http://127.0.0.1:{port}"]
    for ip in _local_lan_ips():
        url = f"http://{ip}:{port}"
        if url not in urls:
            urls.append(url)
    if len(urls) == 1:
        urls.append(f"http://<本机局域网IP>:{port}")
    return urls


def _local_lan_ips() -> list[str]:
    ips: set[str] = set()
    try:
        hostname = socket.gethostname()
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            ip = str(item[4][0])
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
            if ip and not ip.startswith("127."):
                ips.add(ip)
    except Exception:
        pass
    return sorted(ips)


HTML_PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Protocol Reg 账号管理</title>
  <style>
    :root {
      --ink: #11100d;
      --paper: #f5efe2;
      --paper-2: #ebe1cf;
      --line: rgba(17, 16, 13, .16);
      --muted: #6f6659;
      --accent: #d8612c;
      --accent-2: #0f6b5f;
      --good: #2d7a46;
      --warn: #a95f00;
      --bad: #a33b2f;
      --shadow: 0 24px 80px rgba(48, 39, 25, .18);
      --mono: "JetBrains Mono", "Cascadia Code", "SFMono-Regular", Menlo, Consolas, monospace;
      --display: "Fraunces", "Iowan Old Style", Georgia, serif;
      --body: "Aptos", "Gill Sans", "Trebuchet MS", sans-serif;
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: var(--body);
      background:
        radial-gradient(circle at 20% 10%, rgba(216, 97, 44, .22), transparent 30%),
        radial-gradient(circle at 95% 0%, rgba(15, 107, 95, .18), transparent 28%),
        linear-gradient(135deg, #f8f0df 0%, #efe4d2 52%, #e3d3bd 100%);
      overflow-x: hidden;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: .35;
      background-image:
        linear-gradient(rgba(17,16,13,.045) 1px, transparent 1px),
        linear-gradient(90deg, rgba(17,16,13,.045) 1px, transparent 1px);
      background-size: 26px 26px;
      mask-image: linear-gradient(to bottom, black, transparent 85%);
    }

    .shell {
      width: min(1440px, calc(100vw - 32px));
      margin: 0 auto;
      padding: 28px 0 36px;
      position: relative;
    }

    .topbar, .stat-card, .panel {
      border: 1px solid var(--line);
      background: rgba(245, 239, 226, .76);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }

    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 12px 20px;
      margin-bottom: 16px;
      border-radius: 22px;
      position: relative;
      overflow: hidden;
      animation: rise .45s ease both;
    }

    .topbar::after {
      content: "";
      position: absolute;
      width: 120px;
      height: 120px;
      right: -50px;
      top: -55px;
      border-radius: 50%;
      border: 22px solid rgba(216, 97, 44, .14);
      pointer-events: none;
    }

    .brand {
      display: flex;
      flex-direction: column;
      gap: 3px;
      flex: 0 0 auto;
      position: relative;
      z-index: 1;
    }

    .brand-eyebrow {
      font-family: var(--mono);
      font-size: 10px;
      letter-spacing: .18em;
      color: var(--accent-2);
      text-transform: uppercase;
    }

    .brand-mark {
      font-family: var(--display);
      font-size: 24px;
      line-height: 1;
      letter-spacing: 0;
    }

    .eyebrow {
      font-family: var(--mono);
      letter-spacing: .16em;
      font-size: 12px;
      color: var(--accent-2);
      text-transform: uppercase;
      margin-bottom: 16px;
    }

    .stats {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      justify-content: flex-end;
      position: relative;
      z-index: 1;
    }

    .top-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      flex: 1 1 auto;
      position: relative;
      z-index: 1;
    }

    .page-links {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }

    .nav-main {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 12px;
      flex-wrap: wrap;
    }

    .page-nav {
      min-height: 42px;
      padding-inline: 16px;
      border-radius: 12px;
    }

    .page-nav.is-active {
      background: var(--ink);
      color: #fff;
      box-shadow: 0 12px 28px rgba(17,16,13,.18);
    }

    .stat-card {
      border-radius: 14px;
      padding: 8px 14px;
      min-width: 102px;
      display: flex;
      flex-direction: column;
      gap: 2px;
      animation: rise .55s ease both;
    }

    .stat-card:nth-child(2) { animation-delay: .05s; }
    .stat-card:nth-child(3) { animation-delay: .1s; }
    .stat-card:nth-child(4) { animation-delay: .15s; }
    .stat-card:nth-child(5) { animation-delay: .2s; }

    .stat-label {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 10px;
      letter-spacing: .04em;
    }

    .stat-value {
      font-family: var(--mono);
      font-size: 22px;
      line-height: 1;
      letter-spacing: 0;
      font-variant-numeric: tabular-nums;
    }

    .workspace {
      display: block;
    }

    body[data-page="tasks"] .workspace,
    body[data-page="settings"] .workspace,
    body[data-page="tasks"] #accountModalBackdrop,
    body[data-page="settings"] #accountModalBackdrop,
    body[data-page="tasks"] #stats {
      display: none !important;
    }

    body[data-page="settings"] #stats {
      display: none !important;
    }

    body[data-page="accounts"] #opsModalBackdrop {
      display: none !important;
    }

    body[data-page="tasks"] #opsModalBackdrop,
    body[data-page="settings"] #opsModalBackdrop {
      position: relative;
      inset: auto;
      z-index: auto;
      flex: 1 1 auto;
      min-height: 0;
      display: flex !important;
      align-items: stretch;
      justify-content: stretch;
      padding: 0;
      background: transparent;
      animation: none;
      width: 100%;
      margin: 0;
    }

    body[data-page="tasks"] #opsModalBackdrop .modal {
      width: 100%;
      max-width: none;
      max-height: none;
      height: 100%;
      min-height: 0;
      min-width: 0;
      animation: rise .35s ease both;
    }

    body[data-page="settings"] #opsModalBackdrop .modal {
      width: 100%;
      max-width: 1180px;
      max-height: none;
      height: auto;
      min-height: 0;
      min-width: 0;
      margin: 0 auto;
      animation: rise .35s ease both;
    }

    body[data-page="tasks"] #opsModalCloseBtn,
    body[data-page="settings"] #opsModalCloseBtn {
      display: none;
    }

    body[data-page="tasks"] .auto-panel {
      display: none;
    }

    body[data-page="tasks"] .airgate-panel {
      display: none;
    }

    body[data-page="tasks"] .ops {
      grid-template-columns: minmax(340px, 430px) minmax(0, 1fr);
      align-items: stretch;
      align-content: stretch;
      height: 100%;
    }

    body[data-page="tasks"] .ops-stack {
      display: flex;
      flex-direction: column;
      gap: 14px;
      height: 100%;
      min-height: 0;
      align-content: start;
    }

    body[data-page="tasks"] .ops-row {
      padding: 14px;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 20px;
      background: rgba(255,255,255,.34);
    }

    body[data-page="tasks"] .op-credentials {
      padding: 14px;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 20px;
      background: rgba(255,255,255,.28);
    }

    body[data-page="tasks"] .prompt-box {
      padding: 14px;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 18px;
      background: rgba(255,255,255,.28);
    }

    body[data-page="tasks"] .ops-meta {
      min-height: 0;
      height: 100%;
      display: flex;
      flex-direction: column;
      gap: 10px;
      padding: 14px;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 20px;
      background: rgba(255,255,255,.26);
    }

    body[data-page="tasks"] .job-list {
      max-height: none;
      flex: 1;
      min-height: 0;
    }

    body[data-page="tasks"] .job-console {
      min-height: 0;
      max-height: none;
      flex: 1;
    }

    body[data-page="settings"] .ops {
      grid-template-columns: minmax(0, 1fr);
      grid-template-rows: auto;
      max-width: 980px;
      margin: 0 auto;
      align-content: start;
      align-items: start;
      gap: 14px;
      height: auto;
    }

    body[data-page="settings"] .ops-stack {
      display: none;
    }

    body[data-page="settings"] .ops-meta {
      display: none;
    }

    body[data-page="settings"] .auto-panel,
    body[data-page="settings"] .airgate-panel {
      border-radius: 18px;
      padding: 16px;
      box-shadow: 0 8px 24px rgba(48,39,25,.06);
      min-height: 0;
      align-self: start;
    }

    body[data-page="settings"] .auto-controls,
    body[data-page="settings"] .airgate-controls {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    body[data-page="settings"] .airgate-controls .auto-field:nth-child(1),
    body[data-page="settings"] .airgate-controls .auto-field:nth-child(2),
    body[data-page="settings"] .airgate-controls .auto-field:nth-child(3) {
      grid-column: 1 / -1;
    }

    body[data-page="settings"] .airgate-controls button,
    body[data-page="settings"] .auto-controls button {
      width: 100%;
      min-height: 42px;
    }

    body[data-page="settings"] #jobState {
      display: none;
    }

    body[data-page="settings"] {
      overflow-y: auto;
    }

    body[data-page="settings"] .shell {
      height: auto;
      min-height: 100vh;
    }

    body[data-page="settings"] .panel {
      height: auto;
    }

    .panel {
      border-radius: 30px;
      overflow: hidden;
    }

    .toolbar {
      padding: 14px 16px;
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 250, 241, .42);
    }

    .toolbar input,
    .toolbar select { flex: 0 0 auto; width: auto; }
    .toolbar #search { flex: 1 1 220px; min-width: 200px; }
    .toolbar select { min-width: 130px; }
    .toolbar-spacer { flex: 1 1 0; min-width: 0; }

    .batchbar {
      padding: 10px 16px;
      display: none;
      flex-wrap: wrap;
      gap: 10px;
      align-items: center;
      border-bottom: 1px solid var(--line);
      background: rgba(17, 16, 13, .04);
    }

    .batchbar.show { display: flex; }

    .batch-count {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
    }

    .check-label {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
      padding: 8px 10px;
      border-radius: 999px;
      background: rgba(255,255,255,.34);
      border: 1px solid rgba(17,16,13,.1);
    }

    .check-label input,
    .select-box input {
      width: auto;
      accent-color: var(--accent);
    }

    .modal-backdrop {
      position: fixed;
      inset: 0;
      background: rgba(28, 18, 5, .45);
      z-index: 50;
      display: none;
      align-items: center;
      justify-content: center;
      padding: 24px;
      animation: backdropIn .18s ease;
    }

    .modal-backdrop.is-open { display: flex; }

    .modal-confirm {
      width: min(420px, 100%);
      max-height: none;
    }
    .confirm-head {
      padding: 22px 24px 8px;
    }
    .confirm-title {
      font-family: var(--display);
      font-size: 22px;
      letter-spacing: 0;
      line-height: 1.2;
    }
    .confirm-message {
      margin-top: 10px;
      color: var(--muted);
      font-size: 14px;
      line-height: 1.7;
      white-space: pre-wrap;
    }
    .confirm-actions {
      display: flex;
      justify-content: flex-end;
      gap: 10px;
      padding: 18px 24px 22px;
    }

    .modal {
      width: min(680px, 100%);
      max-height: calc(100vh - 48px);
      background: var(--paper);
      border: 1px solid var(--line);
      border-radius: 28px;
      box-shadow: 0 32px 80px rgba(28, 18, 5, .42);
      display: flex;
      flex-direction: column;
      overflow: hidden;
      animation: modalIn .22s ease;
    }

    .modal .editor-head { flex-shrink: 0; }
    .modal .tab-panel { flex: 1; overflow-y: auto; min-height: 0; }
    .modal .tab-panel.is-hidden { display: none; }
    .modal [data-panel="form"] { padding-bottom: 0; }
    .modal [data-panel="form"] .button-row {
      position: sticky;
      bottom: 0;
      margin: 8px -20px 0;
      padding: 14px 20px 18px;
      background: rgba(245, 239, 226, .94);
      backdrop-filter: blur(8px);
      border-top: 1px solid var(--line);
      z-index: 2;
    }

    .modal-close {
      background: transparent;
      color: var(--ink);
      font-size: 22px;
      line-height: 1;
      padding: 6px 12px;
      border-radius: 12px;
      box-shadow: none;
      font-weight: 400;
    }
    .modal-close:hover { background: rgba(17, 16, 13, .08); transform: none; filter: none; }

    @keyframes backdropIn { from { opacity: 0; } to { opacity: 1; } }
    @keyframes modalIn { from { opacity: 0; transform: translateY(16px) scale(.97); } to { opacity: 1; transform: translateY(0) scale(1); } }

    input, select, textarea {
      width: 100%;
      border: 1px solid rgba(17, 16, 13, .16);
      background: rgba(255, 252, 246, .74);
      color: var(--ink);
      border-radius: 16px;
      padding: 13px 14px;
      font: 14px var(--body);
      outline: none;
      transition: border-color .18s ease, box-shadow .18s ease, transform .18s ease;
    }

    textarea { resize: vertical; min-height: 140px; font-family: var(--mono); font-size: 12px; line-height: 1.6; }
    #subscriptionNote { min-height: 72px; }

    input:focus, select:focus, textarea:focus {
      border-color: rgba(216, 97, 44, .72);
      box-shadow: 0 0 0 4px rgba(216, 97, 44, .12);
    }

    input[readonly], textarea[readonly] {
      background: rgba(255, 252, 246, .4);
      cursor: default;
    }

    .input-wrap {
      position: relative;
    }
    .input-wrap > input { padding-right: 42px; }
    .input-icon {
      position: absolute;
      right: 6px;
      top: 50%;
      transform: translateY(-50%);
      background: transparent;
      color: var(--muted);
      box-shadow: none;
      padding: 6px 9px;
      font-size: 16px;
      line-height: 1;
      border-radius: 10px;
      font-weight: 400;
      min-width: 0;
      cursor: pointer;
    }
    .input-icon:hover {
      background: rgba(17, 16, 13, .08);
      color: var(--ink);
      transform: translateY(-50%);
      filter: none;
    }
    .input-icon:active { transform: translateY(-50%); }
    .input-icon.is-loading { animation: spin .9s linear infinite; transform-origin: center; }

    .copy-on-click {
      cursor: copy;
      transition: border-color .18s ease, background .18s ease;
    }
    input.copy-on-click:hover:not(:disabled) {
      border-color: rgba(216, 97, 44, .42);
      background: rgba(216, 97, 44, .06);
    }
    .copy-on-click.copied {
      border-color: rgba(45, 122, 70, .55);
      background: rgba(45, 122, 70, .1);
    }

    .label-icon {
      background: transparent;
      color: var(--muted);
      box-shadow: none;
      padding: 2px 8px;
      font-size: 14px;
      line-height: 1;
      border-radius: 8px;
      font-weight: 400;
      min-width: 0;
      cursor: pointer;
    }
    .label-icon:hover {
      background: rgba(17, 16, 13, .08);
      color: var(--ink);
      transform: none;
      filter: none;
    }
    .label-icon:active { transform: none; }
    .label-icon.copied { color: var(--good); }
    @keyframes spin {
      from { transform: translateY(-50%) rotate(0); }
      to { transform: translateY(-50%) rotate(360deg); }
    }
    input[readonly]:focus, textarea[readonly]:focus {
      border-color: rgba(17, 16, 13, .16);
      box-shadow: none;
      transform: none;
    }

    button,
    .button-link {
      border: 0;
      border-radius: 16px;
      padding: 12px 15px;
      color: #fff;
      background: var(--ink);
      font: 700 13px var(--body);
      cursor: pointer;
      transition: transform .18s ease, box-shadow .18s ease, filter .18s ease;
      box-shadow: 0 12px 28px rgba(17,16,13,.18);
      white-space: nowrap;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }

    button:hover,
    .button-link:hover { transform: translateY(-2px); filter: brightness(1.05); }
    button:active,
    .button-link:active { transform: translateY(0); }
    button:disabled {
      cursor: not-allowed;
      opacity: .5;
      transform: none;
      filter: none;
      box-shadow: none;
    }
    .secondary { background: var(--accent-2); }
    .ghost { background: rgba(17,16,13,.08); color: var(--ink); box-shadow: none; }
    .danger { background: var(--bad); }
    .accent { background: var(--accent); }

    .list {
      padding: 14px;
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
      grid-auto-rows: max-content;
      align-content: start;
      gap: 14px;
      max-height: calc(100vh - 278px);
      overflow: auto;
    }

    .pagination {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 12px 18px;
      border-top: 1px solid var(--line);
      background: rgba(255, 250, 241, .42);
      font-family: var(--mono);
      font-size: 12px;
      color: var(--muted);
      flex-shrink: 0;
    }

    .pagination .page-info { flex: 1; }
    .pagination .page-current { min-width: 56px; text-align: center; color: var(--ink); }
    .pagination button {
      padding: 7px 13px;
      font-size: 12px;
    }
    .pagination button:disabled { opacity: .45; cursor: not-allowed; }

    .account-card {
      display: flex;
      flex-direction: column;
      gap: 8px;
      padding: 12px;
      border: 1px solid rgba(17,16,13,.11);
      border-radius: 18px;
      background: rgba(255, 252, 246, .58);
      cursor: pointer;
      transition: transform .18s ease, border-color .18s ease, background .18s ease;
      animation: cardIn .28s ease both;
    }

    .account-head {
      display: flex;
      align-items: flex-start;
      gap: 10px;
    }

    .select-box {
      align-self: start;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 24px;
      height: 24px;
      margin-top: 1px;
      cursor: pointer;
    }

    .account-card:hover, .account-card.active {
      transform: translateY(-2px);
      border-color: rgba(216, 97, 44, .5);
      background: rgba(255, 250, 241, .9);
    }

    .email {
      font-family: var(--mono);
      font-size: 14px;
      word-break: break-all;
    }

    .meta {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      color: var(--muted);
      font-size: 12px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 999px;
      padding: 4px 8px;
      background: rgba(255,255,255,.42);
      font-family: var(--mono);
      font-size: 11px;
    }

    .pill.good { color: var(--good); }
    .pill.warn { color: var(--warn); }
    .pill.bad { color: var(--bad); }

    .card-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
    }

    .stock-action {
      padding: 8px 11px;
      border-radius: 999px;
      font: 700 12px var(--body);
      background: var(--accent);
      box-shadow: 0 8px 18px rgba(216, 97, 44, .2);
    }

    .stock-action.out {
      color: var(--ink);
      background: rgba(17,16,13,.08);
      box-shadow: none;
    }

    .card-actions .updated {
      margin: 0;
      font-family: var(--mono);
      font-size: 11px;
      color: rgba(17,16,13,.58);
      white-space: nowrap;
    }

    .card-actions .updated strong {
      color: rgba(17,16,13,.78);
      font-weight: 700;
    }

    .updated {
      align-self: center;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 11px;
      text-align: right;
      min-width: 110px;
    }

    .editor-head {
      padding: 20px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 250, 241, .42);
    }

    .editor-title {
      font-family: var(--display);
      font-size: 28px;
      letter-spacing: 0;
    }

    .header-actions {
      display: inline-flex;
      align-items: center;
      gap: 10px;
    }

    .tabs {
      display: flex;
      gap: 2px;
      padding: 0 14px;
      border-bottom: 1px solid var(--line);
      position: relative;
    }

    .tab {
      position: relative;
      background: transparent;
      color: var(--muted);
      box-shadow: none;
      border-radius: 0;
      padding: 15px 16px 14px;
      font: 600 13px var(--mono);
      letter-spacing: .04em;
      display: inline-flex;
      align-items: center;
      gap: 10px;
      transition: color .18s ease;
    }

    .tab:hover { transform: none; filter: none; color: var(--ink); }
    .tab:active { transform: none; }
    .tab.is-active { color: var(--ink); }
    .tab.is-active::after {
      content: "";
      position: absolute;
      left: 12px; right: 12px;
      bottom: -1px;
      height: 2px;
      background: var(--accent);
      border-radius: 2px 2px 0 0;
    }

    .tab-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      font: 500 11px var(--mono);
      padding: 3px 9px 3px 8px;
      border-radius: 999px;
      background: rgba(17, 16, 13, .07);
      color: var(--muted);
      letter-spacing: 0;
      transition: background .2s ease, color .2s ease;
    }

    .tab-badge::before {
      content: "";
      width: 6px; height: 6px;
      border-radius: 999px;
      background: currentColor;
      flex: none;
    }

    .tab-badge[data-status="running"],
    .tab-badge[data-status="pending"] { color: var(--accent-2); background: rgba(15, 107, 95, .14); }
    .tab-badge[data-status="running"]::before,
    .tab-badge[data-status="pending"]::before { animation: pulse 1.2s ease-in-out infinite; }
    .tab-badge[data-status="waiting"] { color: var(--warn); background: rgba(169, 95, 0, .18); }
    .tab-badge[data-status="waiting"]::before { animation: pulse 1s ease-in-out infinite; }
    .tab-badge[data-status="succeeded"] { color: var(--good); background: rgba(45, 122, 70, .16); }
    .tab-badge[data-status="failed"] { color: var(--bad); background: rgba(163, 59, 47, .16); }

    .tab-panel {
      padding: 20px;
      animation: fadeIn .26s ease;
    }
    .tab-panel.is-hidden { display: none; }

    .form {
      display: grid;
      gap: 14px;
    }

    .ops {
      display: grid;
      gap: 14px;
      min-width: 0;
    }

    .ops-stack {
      display: grid;
      gap: 14px;
      min-width: 0;
    }

    .ops-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 110px auto;
      gap: 12px;
      align-items: stretch;
      min-width: 0;
    }

    .ops-row select { height: 100%; }

    .ops-meta {
      display: grid;
      gap: 10px;
      min-width: 0;
    }

    .ops-summary {
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 16px;
      padding: 10px 12px;
      background: rgba(255,255,255,.34);
      color: var(--muted);
      font: 12px var(--mono);
    }

    .job-list {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(220px, 100%), 1fr));
      gap: 8px;
      max-height: 210px;
      min-width: 0;
      overflow: auto;
      overscroll-behavior: contain;
      padding: 8px;
      border: 1px solid rgba(17,16,13,.1);
      border-radius: 16px;
      background: rgba(255,255,255,.22);
      align-content: start;
    }

    .job-card {
      display: grid;
      gap: 5px;
      text-align: left;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 12px;
      padding: 9px 10px;
      background: rgba(255,255,255,.32);
      color: var(--ink);
      box-shadow: none;
      transform: none;
      min-width: 0;
      width: 100%;
      white-space: normal;
      overflow: hidden;
    }

    .job-card:hover {
      transform: translateY(-1px);
      border-color: rgba(216, 97, 44, .32);
    }

    .job-card.active {
      border-color: rgba(216, 97, 44, .58);
      background: rgba(216, 97, 44, .10);
    }

    .job-card[data-status="running"],
    .job-card[data-status="pending"] {
      border-color: rgba(15, 107, 95, .28);
      background: rgba(15, 107, 95, .08);
    }

    .job-card[data-status="waiting"] {
      border-color: rgba(169, 95, 0, .32);
      background: rgba(169, 95, 0, .08);
    }

    .job-card[data-status="failed"] {
      border-color: rgba(163, 59, 47, .34);
      background: rgba(163, 59, 47, .08);
    }

    .job-card[data-status="succeeded"] {
      border-color: rgba(45, 122, 70, .3);
      background: rgba(45, 122, 70, .07);
    }

    .job-card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      min-width: 0;
    }

    .job-card .tab-badge {
      flex: none;
      padding-inline: 7px;
    }

    .job-card-title {
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
      font: 600 12px var(--mono);
    }

    .job-card-meta {
      color: var(--muted);
      font: 11px var(--mono);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      min-width: 0;
    }

    @keyframes pulse {
      0%, 100% { transform: scale(1); opacity: 1; }
      50% { transform: scale(1.6); opacity: .45; }
    }

    @keyframes fadeIn {
      from { opacity: 0; transform: translateY(6px); }
      to { opacity: 1; transform: translateY(0); }
    }

    .checks {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
    }

    .checks label {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid rgba(17,16,13,.1);
      border-radius: 999px;
      padding: 8px 10px;
      background: rgba(255,255,255,.32);
    }

    .checks input { width: auto; }

    .auto-panel {
      display: grid;
      gap: 12px;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 20px;
      padding: 14px;
      background:
        linear-gradient(135deg, rgba(216,97,44,.12), rgba(15,107,95,.09)),
        rgba(255,255,255,.34);
      min-width: 0;
    }

    .airgate-panel {
      display: grid;
      gap: 12px;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 20px;
      padding: 14px;
      background:
        linear-gradient(135deg, rgba(15,107,95,.10), rgba(216,97,44,.08)),
        rgba(255,255,255,.34);
      min-width: 0;
    }

    .auto-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 12px;
    }

    .auto-title {
      font-family: var(--display);
      font-size: 20px;
      line-height: 1;
      letter-spacing: 0;
    }

    .auto-controls {
      display: grid;
      grid-template-columns: repeat(2, minmax(110px, 1fr)) minmax(170px, auto) auto auto;
      gap: 10px;
      align-items: end;
    }

    .airgate-controls {
      display: grid;
      grid-template-columns: repeat(3, minmax(110px, 1fr)) repeat(4, auto);
      gap: 10px;
      align-items: end;
    }

    .auto-field {
      display: grid;
      gap: 6px;
      color: var(--muted);
      font: 11px var(--mono);
    }

    .auto-field input { width: 100%; }
    .airgate-controls .auto-field input { width: 100%; }

    .auto-summary {
      color: var(--muted);
      font: 12px/1.55 var(--mono);
    }

    .job-console {
      min-height: 190px;
      max-height: 310px;
      overflow: auto;
      border-radius: 18px;
      padding: 14px;
      background: #16130f;
      color: #f4ead8;
      font: 12px/1.65 var(--mono);
      white-space: pre-wrap;
      overflow-wrap: anywhere;
      min-width: 0;
      width: 100%;
      border: 1px solid rgba(255,255,255,.08);
    }

    .prompt-box {
      display: none;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 10px;
      align-items: center;
    }

    .prompt-box.show { display: grid; }

    .field label {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
    }

    .field-actions {
      display: inline-flex;
      flex-wrap: wrap;
      justify-content: flex-end;
      gap: 8px;
    }

    .grid-2 { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .button-row { display: flex; flex-wrap: wrap; gap: 10px; }

    .empty {
      padding: 54px 22px;
      color: var(--muted);
      text-align: center;
      line-height: 1.8;
    }

    .toast {
      position: fixed;
      right: 22px;
      bottom: 22px;
      padding: 14px 16px;
      border-radius: 18px;
      color: #fff;
      background: var(--ink);
      box-shadow: var(--shadow);
      opacity: 0;
      transform: translateY(12px);
      pointer-events: none;
      transition: opacity .2s ease, transform .2s ease;
      z-index: 10;
    }

    .toast.show { opacity: 1; transform: translateY(0); }

    @keyframes rise {
      from { opacity: 0; transform: translateY(18px); }
      to { opacity: 1; transform: translateY(0); }
    }

    @keyframes cardIn {
      from { opacity: 0; transform: translateY(8px); }
      to { opacity: 1; transform: translateY(0); }
    }

    @media (min-width: 1121px) {
      body { overflow: hidden; }
      .shell {
        height: 100vh;
        display: flex;
        flex-direction: column;
      }
      .topbar { flex: 0 0 auto; }
      .workspace { flex: 1; min-height: 0; }
      .panel {
        height: 100%;
        min-height: 0;
        display: flex;
        flex-direction: column;
      }
      .toolbar { flex-shrink: 0; }
      .list {
        flex: 1;
        max-height: none;
        min-height: 0;
      }
    }

    @media (max-width: 1120px) {
      .list { max-height: none; }
    }

    @media (max-width: 760px) {
      .shell { width: min(100vw - 20px, 1440px); padding-top: 10px; }
      .topbar { align-items: stretch; }
      .stats { justify-content: flex-start; }
      .top-actions { justify-content: flex-start; }
      .nav-main { justify-content: flex-start; width: 100%; }
      .grid-2 { grid-template-columns: 1fr; }
      .ops-row { grid-template-columns: 1fr; }
      .auto-controls,
      .airgate-controls { grid-template-columns: 1fr; }
      .auto-head { align-items: flex-start; flex-direction: column; }
      body[data-page="tasks"] #opsModalBackdrop,
      body[data-page="settings"] #opsModalBackdrop {
        width: min(100vw - 20px, 1440px);
      }
      body[data-page="tasks"] .ops {
        grid-template-columns: 1fr;
      }
      .list { grid-template-columns: 1fr; }
      .card-actions { justify-content: flex-start; flex-wrap: wrap; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-eyebrow">Protocol Reg</div>
        <div class="brand-mark" id="brandMark">账号库控制台</div>
      </div>
      <div class="top-actions">
        <nav class="page-links" aria-label="页面导航">
          <a class="button-link ghost page-nav" id="accountsNavLink" href="/">账号管理</a>
          <a class="button-link ghost page-nav" id="tasksNavLink" href="/tasks">任务页面</a>
          <a class="button-link ghost page-nav" id="settingsNavLink" href="/settings">设置</a>
          <a class="button-link ghost page-nav" id="operatorNavLink" href="/operator">订阅处理</a>
          <a class="button-link ghost page-nav" id="adminUsersNavLink" href="/admin/users">用户管理</a>
        </nav>
        <div class="nav-main">
          <span class="tab-badge" id="currentUserBadge">未登录</span>
          <button class="ghost" type="button" id="logoutBtn">退出</button>
        </div>
      </div>
    </header>

    <div class="stats" id="stats"></div>

    <section class="workspace">
      <div class="panel">
        <div class="toolbar">
          <input id="search" placeholder="搜索邮箱 / 类型 / 订阅 / 出库状态" autocomplete="off" />
          <select id="planFilter"><option value="all">全部类型</option></select>
          <select id="statusFilter"><option value="all">全部状态</option></select>
          <select id="subscriptionFilter"><option value="all">全部订阅</option></select>
          <select id="stockFilter"><option value="all">全部库存</option></select>
          <select id="pageSizeSelect" title="每页条数">
            <option value="20">20 / 页</option>
            <option value="50" selected>50 / 页</option>
            <option value="100">100 / 页</option>
            <option value="200">200 / 页</option>
          </select>
          <span class="toolbar-spacer"></span>
          <button class="ghost" id="exportBtn">导出 TXT</button>
          <button class="ghost" id="exportTokensBtn">导出 Token</button>
          <button class="ghost" id="exportCheckoutsBtn">导出 Checkout</button>
          <button class="secondary" id="exportAirgateBtn">导出 AirGate JSON</button>
          <a class="button-link secondary" id="runBtn" href="/tasks">任务页面</a>
        </div>
        <div class="batchbar" id="batchBar">
          <label class="check-label"><input type="checkbox" id="selectAllVisible" /> 全选本页</label>
          <span class="batch-count" id="batchCount">已选择 0 个</span>
          <button class="accent" type="button" id="batchStockOutBtn">批量出库</button>
          <button class="ghost" type="button" id="batchStockInBtn">恢复未出库</button>
          <button class="secondary" type="button" id="batchRefreshPlanBtn">自动获取类型</button>
          <button class="secondary" type="button" id="batchExportJsonlBtn">导出 Session JSONL</button>
          <button class="secondary" type="button" id="batchExportAirgateBtn">导出 AirGate JSON</button>
          <button class="danger" type="button" id="batchDeleteBtn">批量删除</button>
          <button class="ghost" type="button" id="batchClearBtn">清空选择</button>
        </div>
        <div class="list" id="accountList"></div>
        <div class="pagination" id="pagination">
          <span class="page-info" id="pageInfo">共 0 条</span>
          <button class="ghost" type="button" id="prevPageBtn" disabled>上一页</button>
          <span class="page-current" id="pageCurrent">1 / 1</span>
          <button class="ghost" type="button" id="nextPageBtn" disabled>下一页</button>
        </div>
      </div>
    </section>
  <div class="modal-backdrop" id="accountModalBackdrop">
    <div class="modal" role="dialog" aria-modal="true">
      <div class="editor-head">
        <div>
          <div class="editor-title" id="editorTitle">账号详情</div>
          <div class="eyebrow" id="editorHint">仅查看，不可编辑</div>
        </div>
        <button class="modal-close" id="accountModalCloseBtn" type="button" aria-label="关闭">×</button>
      </div>
      <section class="tab-panel" data-panel="form">
        <form class="form" id="accountForm">
          <input type="hidden" id="accountId" />
          <div class="field">
            <label>邮箱 <span>唯一键</span></label>
            <input id="email" class="copy-on-click" readonly placeholder="name@your-domain.com" />
          </div>
          <div class="field">
            <label>密码</label>
            <input id="password" class="copy-on-click" readonly placeholder="账号密码" />
          </div>
          <div class="grid-2">
            <div class="field">
              <label>订阅类型</label>
              <div class="input-wrap">
                <input id="subscriptionType" class="copy-on-click" readonly placeholder="free / plus / team / null" />
                <button class="input-icon" type="button" id="refreshPlanBtn" title="自动获取订阅类型" aria-label="刷新">↻</button>
              </div>
            </div>
            <div class="field">
              <label>出库状态</label>
              <input id="stockStatus" class="copy-on-click" readonly placeholder="未出库 / 出库" />
            </div>
          </div>
          <div class="grid-2">
            <div class="field">
              <label>订阅状态</label>
              <input id="subscriptionStatus" class="copy-on-click" readonly placeholder="待订阅 / 处理中 / 已点击订阅 / 已确认订阅" />
            </div>
            <div class="field">
              <label>领取人</label>
              <input id="subscriptionOperator" class="copy-on-click" readonly placeholder="未领取" />
            </div>
          </div>
          <div class="grid-2">
            <div class="field">
              <label>领取时间</label>
              <input id="subscriptionClaimedAt" class="copy-on-click" readonly placeholder="无记录" />
            </div>
            <div class="field">
              <label>点击时间</label>
              <input id="subscriptionMarkedAt" class="copy-on-click" readonly placeholder="无记录" />
            </div>
          </div>
          <div class="grid-2">
            <div class="field">
              <label>确认时间</label>
              <input id="subscriptionVerifiedAt" class="copy-on-click" readonly placeholder="无记录" />
            </div>
            <div class="field">
              <label>备注</label>
              <textarea id="subscriptionNote" readonly spellcheck="false" placeholder="无备注"></textarea>
            </div>
          </div>
          <div class="grid-2">
            <div class="field">
              <label>自动核实进度</label>
              <input id="subscriptionVerifyProgress" class="copy-on-click" readonly placeholder="自动核实中 / 已确认 / 已暂停" />
            </div>
            <div class="field">
              <label>下次核实</label>
              <input id="subscriptionVerifyNextAt" class="copy-on-click" readonly placeholder="无待执行" />
            </div>
          </div>
          <div class="field">
            <label>状态</label>
            <input id="status" class="copy-on-click" readonly placeholder="active" />
          </div>
          <div class="field">
            <label>Refresh Token</label>
            <input id="refreshToken" class="copy-on-click" readonly placeholder="没有则留空" />
          </div>
          <div class="field">
            <label>Checkout 长链接</label>
            <div class="input-wrap">
              <input id="checkoutUrl" class="copy-on-click" readonly placeholder="生成 checkout 后自动保存" />
              <button class="input-icon" type="button" id="regenCheckoutBtn" title="生成 / 重新生成 checkout" aria-label="生成">+</button>
            </div>
          </div>
          <div class="field">
            <label>
              <span>Session JSON</span>
              <button class="label-icon" type="button" id="refreshSessionBtn" title="重新获取 Session JSON" aria-label="刷新">↻</button>
              <button class="label-icon" type="button" id="copySessionBtn" title="复制 Session JSON" aria-label="复制">⧉</button>
            </label>
            <textarea id="sessionJson" readonly spellcheck="false" placeholder="/api/auth/session 返回 data 对象"></textarea>
          </div>
          <div class="button-row">
            <button class="accent" type="button" id="stockToggleBtn">标记出库</button>
            <button class="secondary" type="button" id="markSubscribedBtn">系统核实订阅</button>
            <button class="ghost" type="button" id="abandonBtn">标记废弃</button>
            <button class="ghost" type="button" id="copyLineBtn">复制账号行</button>
            <button class="danger" type="button" id="deleteBtn">删除</button>
          </div>
        </form>
      </section>
    </div>
  </div>
  <div class="modal-backdrop" id="opsModalBackdrop">
    <div class="modal" role="dialog" aria-modal="true">
      <div class="editor-head">
        <div>
          <div class="editor-title" id="opsTitle">执行任务</div>
          <div class="eyebrow" id="opsHint">注册、登录、授权独立运行</div>
        </div>
        <div class="header-actions">
          <span class="tab-badge" id="jobState" data-status="idle">空闲</span>
          <button class="modal-close" id="opsModalCloseBtn" type="button" aria-label="关闭">×</button>
        </div>
      </div>
      <section class="tab-panel" data-panel="ops">
        <div class="ops">
          <div class="ops-stack">
            <div class="ops-row">
              <select id="opMode">
                <option value="register">register 注册</option>
                <option value="login">login 登录</option>
                <option value="authorize">authorize 授权</option>
              </select>
              <input id="opCount" type="number" min="1" max="20" value="1" title="注册模式下可一次启动多个任务" />
              <button class="secondary" type="button" id="startJobBtn">开始执行</button>
            </div>
            <div class="grid-2 op-credentials">
              <input id="opEmail" placeholder="执行邮箱，留空可使用当前表单" />
              <input id="opPassword" placeholder="执行密码，留空可使用当前表单" />
            </div>
            <div class="prompt-box" id="promptBox">
              <input id="promptInput" placeholder="输入邮箱验证码后继续" />
              <button type="button" id="submitPromptBtn">提交验证码</button>
            </div>
            <div class="job-console" id="jobLog">任务日志会显示在这里。</div>
          </div>
          <div class="auto-panel">
            <div class="auto-head">
              <div>
                <div class="auto-title">自动注册</div>
                <div class="auto-summary" id="autoSummary">未启动</div>
              </div>
              <span class="tab-badge" id="autoState" data-status="idle">未启动</span>
            </div>
            <div class="auto-controls">
              <label class="auto-field">间隔秒数<input id="autoInterval" type="number" min="1" value="300" /></label>
              <label class="auto-field">每轮注册数<input id="autoBatchCount" type="number" min="1" max="20" value="1" /></label>
              <button class="ghost" type="button" id="autoSaveBtn">保存配置</button>
              <button class="secondary" type="button" id="autoStartBtn">启动自动注册</button>
              <button class="ghost" type="button" id="autoStopBtn">停止</button>
            </div>
          </div>
          <div class="airgate-panel">
            <div class="auto-head">
              <div>
                <div class="auto-title">AirGate 401 监控</div>
                <div class="auto-summary" id="airgateSummary">未配置</div>
              </div>
              <span class="tab-badge" id="airgateState" data-status="idle">未配置</span>
            </div>
            <div class="airgate-controls">
              <label class="auto-field">Core 地址<input id="airgateCoreUrl" type="text" placeholder="http://127.0.0.1:8080" /></label>
              <label class="auto-field">Admin Key<input id="airgateAdminKey" type="password" placeholder="admin-..." /></label>
              <label class="auto-field">代理<input id="airgateProxy" type="text" placeholder="可选，留空使用当前任务代理" /></label>
              <label class="auto-field">轮询秒数<input id="airgateInterval" type="number" min="10" value="300" /></label>
              <label class="auto-field">账号冷却<input id="airgateCooldown" type="number" min="60" value="1800" /></label>
              <label class="auto-field">页大小<input id="airgatePageSize" type="number" min="1" max="100" value="100" /></label>
              <button class="ghost" type="button" id="airgateSaveBtn">保存配置</button>
              <button class="secondary" type="button" id="airgateStartBtn">启动监控</button>
              <button class="ghost" type="button" id="airgateRunOnceBtn">立即巡检</button>
              <button class="ghost" type="button" id="airgateStopBtn">停止</button>
            </div>
          </div>
          <div class="ops-meta">
            <div class="ops-summary" id="queueSummary">当前没有运行中的任务</div>
            <div class="job-list" id="jobList"></div>
          </div>
        </div>
      </section>
    </div>
  </div>
  </main>
  <div class="modal-backdrop" id="confirmBackdrop">
    <div class="modal modal-confirm" role="alertdialog" aria-modal="true">
      <div class="confirm-head">
        <div class="confirm-title" id="confirmTitle">确认操作</div>
        <div class="confirm-message" id="confirmMessage"></div>
      </div>
      <div class="confirm-actions">
        <button class="ghost" type="button" id="confirmCancelBtn">取消</button>
        <button class="accent" type="button" id="confirmOkBtn">确认</button>
      </div>
    </div>
  </div>
  <div class="toast" id="toast"></div>

  <script>
    const pageMode = document.body.dataset.page || 'accounts';
    const state = {
      selectedId: null,
      selectedIds: new Set(),
      items: [],
      stats: {},
      jobs: [],
      queue: {},
      auto: {},
      airgate: {},
      currentJobId: null,
      initialJobId: new URLSearchParams(window.location.search).get('job') || '',
      initialAccountId: new URLSearchParams(window.location.search).get('account') || '',
      initialJobApplied: false,
      initialAccountApplied: false,
      returnAccountId: '',
      jobTimer: null,
      refreshAccountIdAfterJob: null,
      page: 1,
      pageSize: 50,
      totalPages: 1,
      total: 0,
    };
    let accountsTimer = null;
    const STOCK_IN = '未出库';
    const STOCK_OUT = '出库';
    const STATUS_ABANDONED = '废弃';
    const $ = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
    const hasValue = (value) => Boolean(String(value ?? '').trim() && String(value ?? '').trim().toLowerCase() !== 'null');

    function setupPageChrome() {
      const brand = $('brandMark');
      const opsTitle = $('opsTitle');
      const opsHint = $('opsHint');
      const titles = {
        accounts: {
          title: '账号管理',
          brand: '账号库控制台',
          opsTitle: '账号管理',
          opsHint: '查看、筛选、导出',
        },
        tasks: {
          title: '任务控制台',
          brand: '账号库控制台',
          opsTitle: '执行任务',
          opsHint: '手动投放注册、登录与授权',
        },
        settings: {
          title: '设置',
          brand: '账号库控制台',
          opsTitle: '自动注册设置',
          opsHint: '保存配置并启停自动注册',
        },
      };
      const links = [
        ['accountsNavLink', '/'],
        ['tasksNavLink', '/tasks'],
        ['settingsNavLink', '/settings'],
        ['operatorNavLink', '/operator'],
        ['adminUsersNavLink', '/admin/users'],
      ];
      const resolved = titles[pageMode] || titles.accounts;
      document.title = `Protocol Reg ${resolved.title}`;
      if (brand) brand.textContent = resolved.brand;
      if (opsTitle) opsTitle.textContent = resolved.opsTitle;
      if (opsHint) opsHint.textContent = resolved.opsHint;
      for (const [id, href] of links) {
        const node = $(id);
        if (!node) continue;
        node.href = href;
        node.classList.toggle('is-active', href === window.location.pathname);
      }
    }

    async function loadCurrentUser() {
      const response = await fetch('/api/auth/me');
      if (response.status === 401) {
        window.location.href = '/login';
        return null;
      }
      if (!response.ok) {
        throw new Error('无法读取登录状态');
      }
      const data = await response.json();
      const user = data.user || null;
      if (user) {
        const role = user.role === 'admin' ? '管理员' : '操作员';
        $('currentUserBadge').textContent = `${user.username} · ${role}`;
      }
      return user;
    }

    function toast(message) {
      const node = $('toast');
      node.textContent = message;
      node.classList.add('show');
      clearTimeout(window.__toastTimer);
      window.__toastTimer = setTimeout(() => node.classList.remove('show'), 2200);
    }

    async function request(url, options = {}) {
      const response = await fetch(url, {
        headers: { 'content-type': 'application/json' },
        ...options,
      });
      if (response.status === 401) {
        window.location.href = '/login';
        throw new Error('未登录或会话已过期');
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '请求失败');
      return data;
    }

    function fmtDate(value) {
      if (!hasValue(value)) return '无记录';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString('zh-CN', { hour12: false });
    }

    function renderStats(stats) {
      const stock = stats.stock_statuses || {};
      const subscription = stats.subscription_statuses || {};
      const operatorStats = Array.isArray(stats.operator_stats) ? stats.operator_stats : [];
      const cards = [
        ['账号总数', stats.total ?? 0],
        ['待订阅', subscription['待订阅'] ?? 0],
        ['处理中', subscription['处理中'] ?? 0],
        ['待核实', subscription['已点击订阅'] ?? 0],
        ['已确认', subscription['已确认订阅'] ?? 0],
        ['活跃操作员', operatorStats.length],
        ['未出库', stock[STOCK_IN] ?? 0],
        ['已出库', stock[STOCK_OUT] ?? 0],
      ];
      $('stats').innerHTML = cards.map(([label, value]) => `
        <div class="stat-card">
          <div class="stat-label">${escapeHtml(label)}</div>
          <div class="stat-value">${escapeHtml(value)}</div>
        </div>
      `).join('');
    }

    function renderFilters(stats) {
      const currentPlan = $('planFilter').value || 'all';
      const currentStatus = $('statusFilter').value || 'all';
      const currentSubscription = $('subscriptionFilter').value || 'all';
      const currentStock = $('stockFilter').value || 'all';
      const planOptions = ['all', ...Object.keys(stats.plans || {}).filter(hasValue)];
      const statusOptions = ['all', ...Object.keys(stats.statuses || {}).filter(hasValue)];
      const subscriptionOptions = ['all', ...Object.keys(stats.subscription_statuses || {}).filter(hasValue)];
      const stockOptions = Array.from(new Set(['all', STOCK_IN, STOCK_OUT, ...Object.keys(stats.stock_statuses || {}).filter(hasValue)]));
      $('planFilter').innerHTML = planOptions.map((value) => `<option value="${escapeHtml(value)}">${value === 'all' ? '全部类型' : escapeHtml(value)}</option>`).join('');
      $('statusFilter').innerHTML = statusOptions.map((value) => `<option value="${escapeHtml(value)}">${value === 'all' ? '全部状态' : escapeHtml(value)}</option>`).join('');
      $('subscriptionFilter').innerHTML = subscriptionOptions.map((value) => `<option value="${escapeHtml(value)}">${value === 'all' ? '全部订阅' : escapeHtml(value)}</option>`).join('');
      $('stockFilter').innerHTML = stockOptions.map((value) => `<option value="${escapeHtml(value)}">${value === 'all' ? '全部库存' : escapeHtml(value)}</option>`).join('');
      $('planFilter').value = planOptions.includes(currentPlan) ? currentPlan : 'all';
      $('statusFilter').value = statusOptions.includes(currentStatus) ? currentStatus : 'all';
      $('subscriptionFilter').value = subscriptionOptions.includes(currentSubscription) ? currentSubscription : 'all';
      $('stockFilter').value = stockOptions.includes(currentStock) ? currentStock : 'all';
    }

    function renderList(items) {
      const list = $('accountList');
      if (!items.length) {
        list.innerHTML = '<div class="empty">没有匹配账号。进入「任务页面」运行 register 来注册账号，或点击「导出 TXT」下载当前数据库。</div>';
        return;
      }
      list.innerHTML = items.map((item, index) => {
        const active = Number(item.id) === Number(state.selectedId) ? ' active' : '';
        const checked = state.selectedIds.has(Number(item.id)) ? ' checked' : '';
        const rtClass = item.has_refresh_token ? 'good' : 'warn';
        const sessionClass = item.has_session ? 'good' : 'warn';
        const checkoutClass = item.has_checkout_url ? 'good' : 'warn';
        const stockStatus = item.stock_status || STOCK_IN;
        const accountStatus = item.status || 'active';
        const subscriptionStatus = item.subscription_status || '待订阅';
        const subscriptionClass = subscriptionStatus === '已确认订阅' ? 'good' : (subscriptionStatus === '订阅失败' ? 'bad' : 'warn');
        const operatorLabel = item.subscription_operator_name || (hasValue(item.subscription_operator_id) ? `#${item.subscription_operator_id}` : '未领取');
        const stockClass = stockStatus === STOCK_OUT ? 'bad' : 'good';
        const stockButtonText = accountStatus === STATUS_ABANDONED
          ? (stockStatus === STOCK_OUT ? '恢复未出库' : '废弃')
          : (stockStatus === STOCK_OUT ? '恢复未出库' : '出库');
        const stockButtonDisabled = accountStatus === STATUS_ABANDONED && stockStatus !== STOCK_OUT;
        return `
          <article class="account-card${active}" data-id="${item.id}" style="animation-delay:${Math.min(index * 18, 180)}ms">
            <div class="account-head">
              <label class="select-box" title="选择账号">
                <input type="checkbox" data-select-id="${item.id}"${checked} />
              </label>
              <div style="min-width:0; flex: 1">
                <div class="email">${escapeHtml(item.email)}</div>
              </div>
            </div>
            <div class="meta">
              <span class="pill ${subscriptionClass}">订阅 · ${escapeHtml(subscriptionStatus)}</span>
              <span class="pill">领取人 · ${escapeHtml(operatorLabel)}</span>
              <span class="pill">${escapeHtml(item.subscription_type || 'null')}</span>
              <span class="pill">${escapeHtml(item.status || 'null')}</span>
              <span class="pill ${stockClass}">库存 · ${escapeHtml(stockStatus)}</span>
              <span class="pill ${checkoutClass}">Checkout · ${item.has_checkout_url ? '有' : '无'}</span>
              <span class="pill ${rtClass}">RT · ${item.has_refresh_token ? '有' : '无'}</span>
              <span class="pill ${sessionClass}">Session · ${item.has_session ? '有' : '无'}</span>
            </div>
            <div class="card-actions">
              <div class="updated"><strong>创建</strong> ${escapeHtml(fmtDate(item.created_at))}</div>
              <button class="stock-action ${stockStatus === STOCK_OUT ? 'out' : ''}" type="button" data-stock-id="${item.id}" ${stockButtonDisabled ? 'disabled' : ''}>${escapeHtml(stockButtonText)}</button>
            </div>
          </article>
        `;
      }).join('');
      list.querySelectorAll('.account-card').forEach((node) => {
        node.addEventListener('click', () => selectAccount(Number(node.dataset.id)));
      });
      list.querySelectorAll('[data-stock-id]').forEach((button) => {
        button.addEventListener('click', (event) => {
          event.stopPropagation();
          toggleStockStatus(Number(button.dataset.stockId)).catch((err) => toast(err.message));
        });
      });
      list.querySelectorAll('.select-box').forEach((label) => {
        label.addEventListener('click', (event) => event.stopPropagation());
      });
      list.querySelectorAll('[data-select-id]').forEach((checkbox) => {
        checkbox.addEventListener('click', (event) => event.stopPropagation());
        checkbox.addEventListener('change', () => {
          const id = Number(checkbox.dataset.selectId);
          if (checkbox.checked) state.selectedIds.add(id);
          else state.selectedIds.delete(id);
          renderBatchBar();
        });
      });
      renderBatchBar();
    }

    function selectedIds() {
      return Array.from(state.selectedIds).filter((id) => Number.isFinite(id) && id > 0);
    }

    function renderBatchBar() {
      const ids = selectedIds();
      const visibleIds = new Set(state.items.map((item) => Number(item.id)));
      const selectedVisibleCount = ids.filter((id) => visibleIds.has(id)).length;
      const allVisibleSelected = state.items.length > 0 && selectedVisibleCount === state.items.length;
      $('batchBar').classList.toggle('show', ids.length > 0);
      $('batchCount').textContent = `已选择 ${ids.length} 个`;
      $('selectAllVisible').checked = allVisibleSelected;
      $('selectAllVisible').indeterminate = selectedVisibleCount > 0 && selectedVisibleCount < state.items.length;
      for (const id of ['batchStockOutBtn', 'batchStockInBtn', 'batchRefreshPlanBtn', 'batchExportJsonlBtn', 'batchExportAirgateBtn', 'batchDeleteBtn', 'batchClearBtn']) {
        $(id).disabled = ids.length === 0;
      }
    }

    function clearSelection() {
      state.selectedIds.clear();
      renderList(state.items);
    }

    function toggleSelectAllVisible(checked) {
      for (const item of state.items) {
        const id = Number(item.id);
        if (checked) state.selectedIds.add(id);
        else state.selectedIds.delete(id);
      }
      renderList(state.items);
    }

    async function loadAccounts() {
      const params = new URLSearchParams({
        search: $('search').value.trim(),
        plan: $('planFilter').value || 'all',
        status: $('statusFilter').value || 'all',
        subscription_status: $('subscriptionFilter').value || 'all',
        stock_status: $('stockFilter').value || 'all',
        page: String(state.page),
        page_size: String(state.pageSize),
      });
      const data = await request(`/api/accounts?${params.toString()}`);
      state.items = data.items || [];
      state.stats = data.stats || {};
      const pagination = data.pagination || {};
      state.page = pagination.page || 1;
      state.pageSize = pagination.page_size || state.pageSize;
      state.total = pagination.total || 0;
      state.totalPages = pagination.total_pages || 1;
      renderStats(state.stats);
      renderFilters(state.stats);
      renderList(state.items);
      renderPagination();
      if (!state.initialAccountApplied && state.initialAccountId) {
        state.initialAccountApplied = true;
        const accountId = Number(state.initialAccountId);
        if (Number.isFinite(accountId) && accountId > 0) {
          selectAccount(accountId).catch((err) => toast(err.message));
        }
      }
    }

    function startAccountsPolling() {
      if (accountsTimer) return;
      accountsTimer = setInterval(() => {
        if (!document.hidden) {
          loadAccounts()
            .then(() => {
              if (state.selectedId !== null) {
                return selectAccount(Number(state.selectedId)).catch(() => {});
              }
              return null;
            })
            .catch(() => {});
        }
      }, 20000);
    }

    function renderPagination() {
      const start = state.total === 0 ? 0 : (state.page - 1) * state.pageSize + 1;
      const end = Math.min(state.total, state.page * state.pageSize);
      $('pageInfo').textContent = state.total === 0
        ? '共 0 条'
        : `共 ${state.total} 条 · ${start}-${end}`;
      $('pageCurrent').textContent = `${state.page} / ${state.totalPages}`;
      $('prevPageBtn').disabled = state.page <= 1;
      $('nextPageBtn').disabled = state.page >= state.totalPages;
    }

    function openAccountModal() {
      $('accountModalBackdrop').classList.add('is-open');
    }

    function closeAccountModal() {
      $('accountModalBackdrop').classList.remove('is-open');
      if (state.selectedId !== null) {
        state.selectedId = null;
        renderList(state.items);
      }
    }

    function openOpsModal() {
      if (pageMode !== 'tasks') return;
      $('opsModalBackdrop').classList.add('is-open');
    }

    function closeOpsModal() {
      if (pageMode === 'tasks') return;
      $('opsModalBackdrop').classList.remove('is-open');
    }

    async function selectAccount(id) {
      const data = await request(`/api/accounts/${id}`);
      const item = data.item;
      openAccountModal();
      state.selectedId = item.id;
      $('accountId').value = item.id;
      $('email').value = item.email || '';
      $('password').value = item.password === 'null' ? '' : item.password || '';
      $('subscriptionType').value = item.subscription_type === 'null' ? '' : item.subscription_type || '';
      $('stockStatus').value = item.stock_status || STOCK_IN;
      $('subscriptionStatus').value = item.subscription_status || '待订阅';
      $('subscriptionOperator').value = item.subscription_operator_name || (item.subscription_operator_id ? `#${item.subscription_operator_id}` : '未领取');
      $('subscriptionClaimedAt').value = fmtDate(item.subscription_claimed_at);
      $('subscriptionMarkedAt').value = fmtDate(item.subscription_marked_at);
      $('subscriptionVerifiedAt').value = fmtDate(item.subscription_verified_at);
      $('subscriptionNote').value = item.subscription_note === 'null' ? '' : item.subscription_note || '';
      $('subscriptionVerifyProgress').value = subscriptionVerifyProgress(item);
      $('subscriptionVerifyNextAt').value = subscriptionVerifyNextLabel(item);
      $('refreshToken').value = item.refresh_token === 'null' ? '' : item.refresh_token || '';
      $('checkoutUrl').value = item.checkout_url === 'null' ? '' : item.checkout_url || '';
      $('status').value = item.status === 'null' ? 'active' : item.status || 'active';
      $('sessionJson').value = item.session_json === 'null' ? '' : prettyJson(item.session_json || '');
      $('opEmail').value = item.email || '';
      $('opPassword').value = item.password === 'null' ? '' : item.password || '';
      $('editorTitle').textContent = item.email || '编辑账号';
      $('editorHint').textContent = accountEditorHint(item);
      updateStockButton(item.stock_status || STOCK_IN, item.id, item.status);
      updateCheckoutButton(item.id);
      updateSessionButton(item.id);
      updatePlanButton(item.id);
      updateMarkSubscribedButton(item.id, '系统核实订阅', item.subscription_status);
      updateAbandonButton(item.id, item.subscription_type, item.status);
      renderList(state.items);
    }

    function prettyJson(value) {
      if (!hasValue(value)) return '';
      try { return JSON.stringify(JSON.parse(value), null, 2); }
      catch { return value; }
    }

    function subscriptionVerifyProgress(item) {
      const status = item.subscription_status || '待订阅';
      const attempts = Number(item.subscription_verify_attempts || 0);
      const lastMessage = hasValue(item.subscription_verify_last_message) ? item.subscription_verify_last_message : '';
      const readableMessage = lastMessage.startsWith('当前订阅类型为 ')
        ? `当前仍为 ${lastMessage.replace('当前订阅类型为 ', '')}`
        : lastMessage;
      if (status === '已点击订阅') {
        const parts = ['自动核实中'];
        if (attempts > 0) parts.push(`第 ${attempts} 次`);
        if (readableMessage) parts.push(readableMessage);
        return parts.join(' · ');
      }
      if (status === '已确认订阅') {
        return readableMessage || '已确认';
      }
      if (status === '订阅失败') {
        return readableMessage ? `已暂停 · ${readableMessage}` : '已暂停';
      }
      if (attempts > 0 || readableMessage) {
        const parts = [];
        if (attempts > 0) parts.push(`核实 ${attempts} 次`);
        if (readableMessage) parts.push(readableMessage);
        return parts.join(' · ');
      }
      return '无自动核实记录';
    }

    function subscriptionVerifyNextLabel(item) {
      const value = item.subscription_verify_next_at;
      return hasValue(value) ? fmtDate(value) : '无待执行';
    }

    function accountEditorHint(item) {
      const operator = item.subscription_operator_name || (item.subscription_operator_id ? `#${item.subscription_operator_id}` : '未领取');
      return [
        `创建 ${fmtDate(item.created_at)}`,
        `更新 ${fmtDate(item.updated_at)}`,
        `订阅 ${item.subscription_status || '待订阅'}`,
        subscriptionVerifyProgress(item),
        `领取人 ${operator}`,
      ].join(' · ');
    }

    function formPayload() {
      return {
        email: $('email').value.trim(),
        password: $('password').value,
        subscription_type: $('subscriptionType').value.trim() || 'null',
        refresh_token: $('refreshToken').value.trim() || 'null',
        session_json: $('sessionJson').value.trim() || 'null',
        checkout_url: $('checkoutUrl').value.trim() || 'null',
        status: $('status').value.trim() || 'active',
        stock_status: $('stockStatus').value || STOCK_IN,
      };
    }

    function updateStockButton(status, id = $('accountId').value, accountStatus = $('status').value) {
      const button = $('stockToggleBtn');
      const abandoned = String(accountStatus || '').trim() === STATUS_ABANDONED;
      const out = status === STOCK_OUT;
      button.disabled = !id || (abandoned && !out);
      if (abandoned && !out) {
        button.textContent = '废弃账号';
        button.title = '废弃账号不允许出库';
      } else {
        button.textContent = out ? '恢复未出库' : '标记出库';
        button.title = '';
      }
    }

    function updateCheckoutButton(id = $('accountId').value) {
      $('regenCheckoutBtn').disabled = !id;
    }

    function updateSessionButton(id = $('accountId').value) {
      $('refreshSessionBtn').disabled = !id;
    }

    function updatePlanButton(id = $('accountId').value) {
      $('refreshPlanBtn').disabled = !id;
    }

    function updateMarkSubscribedButton(id = $('accountId').value, label = '系统核实订阅', subscriptionStatus = $('subscriptionStatus').value) {
      const button = $('markSubscribedBtn');
      const status = String(subscriptionStatus || '').trim();
      const allowed = status === '已点击订阅' || status === '已确认订阅';
      button.disabled = !id || !allowed;
      button.textContent = label;
      button.title = allowed ? '' : '仅在已点击订阅后可核实';
    }

    function updateAbandonButton(id = $('accountId').value, subscriptionType = $('subscriptionType').value, status = $('status').value) {
      const button = $('abandonBtn');
      const abandoned = String(status || '').trim() === STATUS_ABANDONED;
      button.disabled = !id || abandoned;
      button.textContent = abandoned ? '已废弃' : '标记废弃';
      if (!id) button.title = '请先选择账号';
      else if (abandoned) button.title = '账号已标记废弃';
      else button.title = '标记为废弃账号';
    }

    function resetForm() {
      state.selectedId = null;
      $('accountForm').reset();
      $('accountId').value = '';
      $('status').value = 'active';
      $('stockStatus').value = STOCK_IN;
      $('subscriptionStatus').value = '待订阅';
      $('subscriptionOperator').value = '未领取';
      $('subscriptionClaimedAt').value = '';
      $('subscriptionMarkedAt').value = '';
      $('subscriptionVerifiedAt').value = '';
      $('subscriptionNote').value = '';
      $('subscriptionVerifyProgress').value = '';
      $('subscriptionVerifyNextAt').value = '';
      $('checkoutUrl').value = '';
      $('opEmail').value = '';
      $('opPassword').value = '';
      $('editorTitle').textContent = '账号详情';
      $('editorHint').textContent = '仅查看，不可编辑';
      updateStockButton(STOCK_IN, '', 'active');
      updateCheckoutButton('');
      updateSessionButton('');
      updatePlanButton('');
      updateMarkSubscribedButton('', '系统核实订阅', '待订阅');
      updateAbandonButton('', '', 'active');
      renderList(state.items);
    }

    async function toggleStockStatus(accountId = Number($('accountId').value || 0)) {
      if (!accountId) return toast('请先选择或保存账号');
      const selectedId = Number($('accountId').value || 0);
      const listItem = state.items.find((item) => Number(item.id) === Number(accountId));
      const current = selectedId === Number(accountId) ? $('stockStatus').value : (listItem?.stock_status || STOCK_IN);
      const accountStatus = selectedId === Number(accountId) ? $('status').value : (listItem?.status || 'active');
      if (String(accountStatus || '').trim() === STATUS_ABANDONED && current !== STOCK_OUT) {
        return toast('废弃账号不允许出库');
      }
      const nextStatus = current === STOCK_OUT ? STOCK_IN : STOCK_OUT;
      const action = nextStatus === STOCK_OUT ? '标记出库' : '恢复为未出库';
      const email = listItem?.email ? `「${listItem.email}」` : '该账号';
      const ok = await showConfirm(`将${email}${action}。`, { title: action, okText: '确认' });
      if (!ok) return;
      const data = await request(`/api/accounts/${accountId}/stock-status`, {
        method: 'PATCH',
        body: JSON.stringify({ stock_status: nextStatus }),
      });
      toast(nextStatus === STOCK_OUT ? '已标记出库' : '已恢复未出库');
      await loadAccounts();
      if (selectedId === Number(accountId)) {
        const item = data.item;
        $('stockStatus').value = item.stock_status || STOCK_IN;
        $('subscriptionVerifyProgress').value = subscriptionVerifyProgress(item);
        $('subscriptionVerifyNextAt').value = subscriptionVerifyNextLabel(item);
        $('editorHint').textContent = accountEditorHint(item);
        updateStockButton(item.stock_status || STOCK_IN, item.id, item.status);
      }
    }

    async function refreshSubscriptionType() {
      const accountId = Number($('accountId').value || 0);
      if (!accountId) return toast('请先选择账号');
      const button = $('refreshPlanBtn');
      button.disabled = true;
      button.classList.add('is-loading');
      try {
        const data = await request(`/api/accounts/${accountId}/subscription-type/refresh`, { method: 'PATCH' });
        const item = data.item;
        $('subscriptionType').value = item.subscription_type === 'null' ? '' : item.subscription_type || '';
        $('subscriptionVerifyProgress').value = subscriptionVerifyProgress(item);
        $('subscriptionVerifyNextAt').value = subscriptionVerifyNextLabel(item);
        $('editorHint').textContent = accountEditorHint(item);
        updateAbandonButton(item.id, item.subscription_type, item.status);
        toast(`订阅类型已更新为 ${item.subscription_type || 'null'}`);
        await loadAccounts();
        state.selectedId = item.id;
        renderList(state.items);
      } finally {
        button.classList.remove('is-loading');
        updatePlanButton();
      }
    }

    async function markSubscribed() {
      const accountId = Number($('accountId').value || 0);
      if (!accountId) return toast('请先选择账号');
      const button = $('markSubscribedBtn');
      button.disabled = true;
      button.textContent = '核实中...';
      try {
        const data = await request(`/api/admin/subscriptions/${accountId}/verify`, { method: 'POST' });
        const item = data.item;
        $('subscriptionType').value = item.subscription_type === 'null' ? '' : item.subscription_type || '';
        $('subscriptionStatus').value = item.subscription_status || '已确认订阅';
        $('subscriptionOperator').value = item.subscription_operator_name || (item.subscription_operator_id ? `#${item.subscription_operator_id}` : '未领取');
        $('subscriptionClaimedAt').value = fmtDate(item.subscription_claimed_at);
        $('subscriptionMarkedAt').value = fmtDate(item.subscription_marked_at);
        $('subscriptionVerifiedAt').value = fmtDate(item.subscription_verified_at);
        $('subscriptionNote').value = item.subscription_note === 'null' ? '' : item.subscription_note || '';
        $('subscriptionVerifyProgress').value = subscriptionVerifyProgress(item);
        $('subscriptionVerifyNextAt').value = subscriptionVerifyNextLabel(item);
        $('editorHint').textContent = accountEditorHint(item);
        updateAbandonButton(item.id, item.subscription_type, item.status);
        await loadAccounts();
        state.selectedId = item.id;
        renderList(state.items);
        if (data.verified) toast(`已核实为 ${data.plan_type || '已确认订阅'}`);
        else toast(`刷新后仍是 ${data.plan_type || 'null'}`);
      } finally {
        updateMarkSubscribedButton(undefined, '系统核实订阅', $('subscriptionStatus').value);
      }
    }

    async function markAbandoned() {
      const accountId = Number($('accountId').value || 0);
      if (!accountId) return toast('请先选择账号');
      const email = ($('email').value || '').trim();
      const target = email ? `「${email}」` : '该账号';
      const ok = await showConfirm(`将${target}标记为废弃。`, { title: '标记废弃', okText: '确认标记' });
      if (!ok) return;
      const button = $('abandonBtn');
      button.disabled = true;
      button.textContent = '标记中...';
      try {
        const data = await request(`/api/accounts/${accountId}/status/abandon`, { method: 'PATCH' });
        const item = data.item;
        $('status').value = item.status === 'null' ? 'active' : item.status || 'active';
        $('stockStatus').value = item.stock_status || STOCK_IN;
        $('subscriptionVerifyProgress').value = subscriptionVerifyProgress(item);
        $('subscriptionVerifyNextAt').value = subscriptionVerifyNextLabel(item);
        $('editorHint').textContent = accountEditorHint(item);
        updateStockButton(item.stock_status || STOCK_IN, item.id, item.status);
        updateAbandonButton(item.id, item.subscription_type, item.status);
        toast('已标记废弃');
        await loadAccounts();
        state.selectedId = item.id;
        renderList(state.items);
      } finally {
        updateAbandonButton();
      }
    }

    function batchMessage(action, data) {
      const failed = data.failed || [];
      const count = data.updated ?? data.deleted ?? 0;
      return failed.length ? `${action}完成 ${count} 个，失败 ${failed.length} 个` : `${action}完成 ${count} 个`;
    }

    async function batchUpdateStockStatus(stockStatus) {
      const ids = selectedIds();
      if (!ids.length) return toast('请先选择账号');
      const action = stockStatus === STOCK_OUT ? '批量出库' : '恢复为未出库';
      const ok = await showConfirm(`将对选中的 ${ids.length} 个账号执行${action}。`, { title: action, okText: '确认' });
      if (!ok) return;
      const data = await request('/api/accounts/batch/stock-status', {
        method: 'PATCH',
        body: JSON.stringify({ ids, stock_status: stockStatus }),
      });
      toast(batchMessage(stockStatus === STOCK_OUT ? '批量出库' : '恢复未出库', data));
      await loadAccounts();
    }

    async function batchRefreshSubscriptionTypes() {
      const ids = selectedIds();
      if (!ids.length) return toast('请先选择账号');
      const data = await request('/api/accounts/batch/subscription-type/refresh', {
        method: 'PATCH',
        body: JSON.stringify({ ids }),
      });
      toast(batchMessage('批量自动获取类型', data));
      await loadAccounts();
    }

    function downloadBlob(blob, filename) {
      const link = document.createElement('a');
      const objectUrl = URL.createObjectURL(blob);
      link.href = objectUrl;
      link.download = filename;
      link.rel = 'noopener';
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    }

    function filenameFromDisposition(disposition, fallback) {
      const match = /filename\*=UTF-8''([^;]+)|filename="?([^";]+)"?/i.exec(disposition || '');
      const encoded = match?.[1];
      if (encoded) {
        try { return decodeURIComponent(encoded); } catch (err) { return fallback; }
      }
      return match?.[2] || fallback;
    }

    async function batchExportSessionJsonl() {
      const ids = selectedIds();
      if (!ids.length) return toast('请先选择账号');
      const response = await fetch('/api/accounts/batch/export-jsonl', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      if (response.status === 401) {
        window.location.href = '/login';
        throw new Error('未登录或会话已过期');
      }
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || '导出失败');
      }
      const blob = await response.blob();
      const filename = filenameFromDisposition(
        response.headers.get('content-disposition'),
        `sessions_selected_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}.jsonl`,
      );
      downloadBlob(blob, filename);
      const exported = Number(response.headers.get('x-exported-count') || ids.length) || ids.length;
      const skipped = Number(response.headers.get('x-skipped-count') || 0) || 0;
      toast(skipped > 0
        ? `已导出 ${exported} 个 session，跳过 ${skipped} 个无 session 账号`
        : `已导出 ${exported} 个 session`);
    }

    async function batchExportAirgateJson() {
      const ids = selectedIds();
      if (!ids.length) return toast('请先选择账号');
      const response = await fetch('/api/accounts/batch/export-airgate', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ ids }),
      });
      if (response.status === 401) {
        window.location.href = '/login';
        throw new Error('未登录或会话已过期');
      }
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || '导出失败');
      }
      const blob = await response.blob();
      const filename = filenameFromDisposition(
        response.headers.get('content-disposition'),
        `airgate-accounts_selected_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}.json`,
      );
      downloadBlob(blob, filename);
      const exported = Number(response.headers.get('x-exported-count') || ids.length) || ids.length;
      const skipped = Number(response.headers.get('x-skipped-count') || 0) || 0;
      toast(skipped > 0
        ? `已导出 ${exported} 个 AirGate 账号，跳过 ${skipped} 个无效账号`
        : `已导出 ${exported} 个 AirGate 账号`);
    }

    async function exportDownload(endpoint, fallbackFilename, successLabel) {
      const response = await fetch(endpoint, { method: 'POST' });
      if (response.status === 401) {
        window.location.href = '/login';
        throw new Error('未登录或会话已过期');
      }
      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(errorData.detail || '导出失败');
      }
      const blob = await response.blob();
      const filename = filenameFromDisposition(
        response.headers.get('content-disposition'),
        fallbackFilename,
      );
      downloadBlob(blob, filename);
      const exported = Number(response.headers.get('x-exported-count') || 0) || 0;
      toast(exported > 0 ? `已导出 ${exported} 个${successLabel}` : `已导出${successLabel}`);
    }

    async function exportAccountsTxt() {
      return exportDownload(
        '/api/export',
        `accounts_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}.txt`,
        '账号',
      );
    }

    async function exportTokensJsonl() {
      return exportDownload(
        '/api/export/tokens',
        `tokens_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}.jsonl`,
        'token',
      );
    }

    async function exportCheckoutsJsonl() {
      return exportDownload(
        '/api/export/checkouts',
        `checkout_urls_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}.jsonl`,
        'checkout',
      );
    }

    async function exportAirgateJson() {
      return exportDownload(
        '/api/export/airgate',
        `airgate-accounts_${new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '')}.json`,
        'AirGate 账号',
      );
    }

    async function batchDeleteAccounts() {
      const ids = selectedIds();
      if (!ids.length) return toast('请先选择账号');
      const ok = await showConfirm(`删除选中的 ${ids.length} 个账号。`, { title: '批量删除账号', okText: '删除', danger: true });
      if (!ok) return;
      const data = await request('/api/accounts/batch/delete', {
        method: 'POST',
        body: JSON.stringify({ ids }),
      });
      toast(batchMessage('批量删除', data));
      if (ids.includes(Number(state.selectedId))) {
        resetForm();
        closeAccountModal();
      }
      clearSelection();
      await loadAccounts();
    }

    async function deleteSelected() {
      const id = $('accountId').value;
      if (!id) return toast('当前没有选中账号');
      const email = ($('email').value || '').trim();
      const target = email ? `「${email}」` : '该账号';
      const ok = await showConfirm(`删除${target}。`, { title: '删除账号', okText: '删除', danger: true });
      if (!ok) return;
      await request(`/api/accounts/${id}`, { method: 'DELETE' });
      toast('账号已删除');
      resetForm();
      closeAccountModal();
      await loadAccounts();
    }

    async function clipboardWrite(text) {
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          return;
        } catch (err) { /* fall through to legacy path */ }
      }
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.top = '0';
      textarea.style.left = '0';
      textarea.style.opacity = '0';
      textarea.style.pointerEvents = 'none';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      textarea.setSelectionRange(0, text.length);
      let ok = false;
      try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
      document.body.removeChild(textarea);
      if (!ok) throw new Error('当前环境不支持复制');
    }

    async function copyText(value, message) {
      if (!hasValue(value)) return toast('没有可复制内容');
      await clipboardWrite(value);
      toast(message);
    }

    function compactLine() {
      const payload = formPayload();
      let sessionText = 'null';
      if (hasValue(payload.session_json)) {
        try { sessionText = JSON.stringify(JSON.parse(payload.session_json)); }
        catch { sessionText = payload.session_json.replace(/[\r\n]+/g, ''); }
      }
      return [payload.email, payload.password, payload.subscription_type, payload.refresh_token, sessionText].join('----');
    }

    function currentJob() {
      return state.jobs.find((job) => String(job.id) === String(state.currentJobId)) || null;
    }

    function jobStatusLabel(status) {
      return { pending: '排队中', running: '运行中', waiting: '等待验证码', succeeded: '已完成', failed: '失败' }[status] || (status || '空闲');
    }

    function fmtUnixSeconds(value) {
      const seconds = Number(value || 0);
      if (!seconds) return '无记录';
      return new Date(seconds * 1000).toLocaleString('zh-CN', { hour12: false });
    }

    function secondsLeft(value) {
      const seconds = Number(value || 0);
      if (!seconds) return 0;
      return Math.max(0, Math.ceil(seconds - Date.now() / 1000));
    }

    function renderAutoPanel() {
      const auto = state.auto || {};
      const enabled = Boolean(auto.enabled);
      const badge = $('autoState');
      if (auto.interval_seconds && document.activeElement !== $('autoInterval')) {
        $('autoInterval').value = String(auto.interval_seconds);
      }
      if (auto.batch_count && document.activeElement !== $('autoBatchCount')) {
        $('autoBatchCount').value = String(auto.batch_count);
      }
      badge.textContent = enabled ? '运行中' : '未启动';
      badge.dataset.status = enabled ? 'running' : 'idle';
      $('autoStartBtn').textContent = enabled ? '已启用' : '启动自动注册';
      $('autoStartBtn').disabled = enabled;
      $('autoStopBtn').disabled = !enabled;
      $('autoSaveBtn').disabled = false;
      const checkoutText = '自动生成 checkout';
      if (!enabled) {
        $('autoSummary').textContent = `已保存：每 ${auto.interval_seconds || 300}s 投放 ${auto.batch_count || 1} 个注册任务 · ${checkoutText} · 点击“启动自动注册”开始执行。`;
        return;
      }
      const last = fmtUnixSeconds(auto.last_run_at);
      const nextLeft = secondsLeft(auto.next_run_at);
      const error = auto.last_error ? ` · 最近错误：${auto.last_error}` : '';
      $('autoSummary').textContent = `每 ${auto.interval_seconds}s 投放 ${auto.batch_count} 个注册任务 · ${checkoutText} · 已投放 ${auto.run_count || 0} 轮 · 上次 ${last} · 下次约 ${nextLeft}s 后${error}`;
    }

    function renderAirgatePanel() {
      const airgate = state.airgate || {};
      const enabled = Boolean(airgate.enabled);
      const configured = Boolean(airgate.configured);
      const badge = $('airgateState');
      if (document.activeElement !== $('airgateCoreUrl') && airgate.core_url) {
        $('airgateCoreUrl').value = airgate.core_url;
      }
      if (document.activeElement !== $('airgateProxy') && hasValue(airgate.proxy)) {
        $('airgateProxy').value = airgate.proxy;
      }
      if (document.activeElement !== $('airgateInterval') && airgate.poll_interval_seconds) {
        $('airgateInterval').value = String(airgate.poll_interval_seconds);
      }
      if (document.activeElement !== $('airgateCooldown') && airgate.account_cooldown_seconds) {
        $('airgateCooldown').value = String(airgate.account_cooldown_seconds);
      }
      if (document.activeElement !== $('airgatePageSize') && airgate.page_size) {
        $('airgatePageSize').value = String(airgate.page_size);
      }
      badge.textContent = enabled ? '运行中' : (configured ? '已配置' : '未配置');
      badge.dataset.status = enabled ? 'running' : (configured ? 'idle' : 'danger');
      $('airgateStartBtn').textContent = enabled ? '已启用' : '启动监控';
      $('airgateStartBtn').disabled = enabled;
      $('airgateStopBtn').disabled = !enabled;
      $('airgateRunOnceBtn').disabled = !configured;
      $('airgateSaveBtn').disabled = false;
      const core = airgate.core_url || '未配置';
      const last = fmtUnixSeconds(airgate.last_run_at);
      const nextLeft = secondsLeft(airgate.next_run_at);
      const error = airgate.last_error ? ` · 最近错误：${airgate.last_error}` : '';
      $('airgateSummary').textContent = configured
        ? `Core ${core} · 冷却 ${airgate.account_cooldown_seconds || 1800}s · 周期 ${airgate.poll_interval_seconds || 300}s · 已巡检 ${airgate.run_count || 0} 轮 · 上次 ${last} · 下次约 ${nextLeft}s 后${error}`
        : '请先配置 core 地址和 admin key';
    }

    function renderQueueSummary() {
      const queue = state.queue || {};
      const active = Number(queue.active || 0);
      const max = Number(queue.max_concurrency || 1);
      const running = Number(queue.running || 0);
      const pending = Number(queue.pending || 0);
      const waiting = Number(queue.waiting || 0);
      const stored = Number(queue.stored || state.jobs.length || 0);
      $('queueSummary').textContent = active
        ? `并发 ${running}/${max} · 排队 ${pending} · 等待验证码 ${waiting} · 活动 ${active} · 历史 ${stored}`
        : '当前没有运行中的任务';
    }

    function renderJobList() {
      const list = $('jobList');
      const jobs = state.jobs || [];
      if (!jobs.length) {
        list.innerHTML = '<div class="empty">暂无任务。点击开始执行后会在这里显示排队、运行和等待验证码的任务。</div>';
        return;
      }
      list.innerHTML = jobs.map((job) => {
        const active = String(job.id) === String(state.currentJobId) ? ' active' : '';
        const status = jobStatusLabel(job.status);
        const statusClass = job.status || 'idle';
        const queueText = job.status === 'pending'
          ? `排队 #${job.queue_position || 0}`
          : status;
        const email = job.email ? escapeHtml(job.email) : '未命名任务';
        return `
          <button type="button" class="job-card${active}" data-job-id="${escapeHtml(job.id)}" data-status="${escapeHtml(statusClass)}">
            <div class="job-card-head">
              <div class="job-card-title">${email}</div>
              <span class="tab-badge" data-status="${escapeHtml(statusClass)}">${escapeHtml(status)}</span>
            </div>
            <div class="job-card-meta">${escapeHtml(job.mode || 'unknown')} · ${escapeHtml(queueText)}</div>
          </button>
        `;
      }).join('');
      list.querySelectorAll('[data-job-id]').forEach((button) => {
        button.addEventListener('click', () => {
          state.currentJobId = button.dataset.jobId || null;
          renderJobList();
          renderJob(currentJob());
        });
      });
    }

    async function loadJobBoard() {
      const data = await request('/api/ops/jobs');
      state.jobs = data.items || [];
      state.queue = data.queue || {};
      state.auto = data.auto || {};
      state.airgate = data.airgate || state.airgate;
      if (!state.initialJobApplied && state.initialJobId && state.jobs.some((job) => String(job.id) === String(state.initialJobId))) {
        state.currentJobId = state.initialJobId;
        state.initialJobApplied = true;
      }
      if (!state.currentJobId && state.jobs.length) {
        state.currentJobId = state.jobs[0].id;
      }
      if (state.currentJobId && !state.jobs.some((job) => String(job.id) === String(state.currentJobId))) {
        const fallback = state.jobs.find((job) => ['pending', 'running', 'waiting'].includes(job.status)) || state.jobs[0] || null;
        state.currentJobId = fallback ? fallback.id : null;
      }
      const selectedBeforeSwitch = currentJob();
      const activeJob = state.jobs.find((job) => ['pending', 'running', 'waiting'].includes(job.status)) || null;
      if (
        activeJob
        && (!selectedBeforeSwitch || (state.auto && state.auto.enabled && ['succeeded', 'failed'].includes(selectedBeforeSwitch.status)))
      ) {
        state.currentJobId = activeJob.id;
      }
      renderQueueSummary();
      renderAutoPanel();
      renderAirgatePanel();
      renderJobList();
      renderJob(currentJob());
      const selected = currentJob();
      const activeCount = Number(state.queue.active || 0);
      const autoEnabled = Boolean(state.auto && state.auto.enabled);
      if (selected && ['succeeded', 'failed'].includes(selected.status) && activeCount === 0 && !autoEnabled) {
        clearInterval(state.jobTimer);
        state.jobTimer = null;
        const refreshId = state.refreshAccountIdAfterJob;
        state.refreshAccountIdAfterJob = null;
        if (pageMode === 'accounts') await loadAccounts();
        if (pageMode === 'accounts' && selected.status === 'succeeded' && refreshId) {
          closeOpsModal();
          window.location.href = `/?account=${encodeURIComponent(refreshId)}`;
          return;
        }
        if (pageMode === 'tasks' && selected.status === 'succeeded') {
          const returnId = state.returnAccountId || refreshId;
          if (returnId) {
            window.location.href = `/?account=${encodeURIComponent(returnId)}`;
            return;
          }
        }
        return;
      }
      if (activeCount === 0 && !autoEnabled) {
        clearInterval(state.jobTimer);
        state.jobTimer = null;
      }
      if ((activeCount > 0 || autoEnabled) && !state.jobTimer) {
        state.jobTimer = setInterval(() => loadJobBoard().catch((err) => toast(err.message)), 1100);
      }
    }

    function jobPayload() {
      const form = formPayload();
      const mode = $('opMode').value;
      const email = $('opEmail').value.trim() || form.email;
      const password = $('opPassword').value || form.password;
      return {
        mode,
        email,
        password,
        account_id: mode === 'register' ? null : ($('accountId').value ? Number($('accountId').value) : null),
        generate_email: true,
        generate_password: true,
        create_checkout: true,
      };
    }

    function jobCount() {
      const raw = Number($('opCount').value || 1);
      if (!Number.isFinite(raw)) return 1;
      return Math.min(20, Math.max(1, Math.trunc(raw)));
    }

    function syncOperationControls() {
      const register = $('opMode').value === 'register';
      $('opCount').disabled = !register;
      if (!register) {
        $('opCount').value = '1';
      }
    }

    function autoRegisterPayload() {
      return {
        interval_seconds: Math.max(1, Math.trunc(Number($('autoInterval').value || 1))),
        batch_count: Math.min(20, Math.max(1, Math.trunc(Number($('autoBatchCount').value || 1)))),
        create_checkout: true,
      };
    }

    async function saveAutoRegisterConfig() {
      const data = await request('/api/ops/auto-register/config', {
        method: 'POST',
        body: JSON.stringify(autoRegisterPayload()),
      });
      state.auto = data.auto || {};
      state.queue = data.queue || state.queue;
      renderAutoPanel();
      renderQueueSummary();
      toast('自动注册配置已保存');
    }

    async function startAutoRegister() {
      const data = await request('/api/ops/auto-register/start', {
        method: 'POST',
        body: JSON.stringify(autoRegisterPayload()),
      });
      state.auto = data.auto || {};
      state.queue = data.queue || state.queue;
      renderAutoPanel();
      renderQueueSummary();
      toast('自动注册已启动');
      startJobPolling();
    }

    async function stopAutoRegister() {
      const data = await request('/api/ops/auto-register/stop', { method: 'POST' });
      state.auto = data.auto || {};
      state.queue = data.queue || state.queue;
      renderAutoPanel();
      renderQueueSummary();
      toast('自动注册已停止');
      await loadJobBoard();
    }

    function airgateMonitorPayload() {
      return {
        core_url: $('airgateCoreUrl').value.trim(),
        admin_key: $('airgateAdminKey').value.trim(),
        proxy: $('airgateProxy').value.trim(),
        poll_interval_seconds: Math.max(10, Math.trunc(Number($('airgateInterval').value || 10))),
        account_cooldown_seconds: Math.max(60, Math.trunc(Number($('airgateCooldown').value || 60))),
        page_size: Math.min(100, Math.max(1, Math.trunc(Number($('airgatePageSize').value || 100)))),
      };
    }

    async function saveAirgateMonitorConfig() {
      const data = await request('/api/ops/airgate-monitor/config', {
        method: 'POST',
        body: JSON.stringify(airgateMonitorPayload()),
      });
      state.airgate = data.airgate || {};
      state.queue = data.queue || state.queue;
      renderAirgatePanel();
      renderQueueSummary();
      toast('AirGate 监控配置已保存');
    }

    async function startAirgateMonitor() {
      const data = await request('/api/ops/airgate-monitor/start', {
        method: 'POST',
        body: JSON.stringify(airgateMonitorPayload()),
      });
      state.airgate = data.airgate || {};
      state.queue = data.queue || state.queue;
      renderAirgatePanel();
      renderQueueSummary();
      toast('AirGate 监控已启动');
    }

    async function stopAirgateMonitor() {
      const data = await request('/api/ops/airgate-monitor/stop', { method: 'POST' });
      state.airgate = data.airgate || {};
      state.queue = data.queue || state.queue;
      renderAirgatePanel();
      renderQueueSummary();
      toast('AirGate 监控已停止');
      await loadJobBoard();
    }

    async function runAirgateMonitorOnce() {
      const data = await request('/api/ops/airgate-monitor/run-once', { method: 'POST' });
      state.airgate = data.airgate || {};
      state.queue = data.queue || state.queue;
      renderAirgatePanel();
      renderQueueSummary();
      toast('AirGate 监控已执行一次巡检');
      await loadJobBoard();
    }

    async function startJob(payloadOverride = null, refreshAccountId = null) {
      const renderInPage = pageMode === 'tasks';
      if (renderInPage) openOpsModal();
      const payload = payloadOverride || jobPayload();
      const batchCount = payloadOverride ? 1 : jobCount();
      const isBatch = payload.mode === 'register' && batchCount > 1;
      const data = await request(isBatch ? '/api/ops/jobs/batch' : '/api/ops/jobs', {
        method: 'POST',
        body: JSON.stringify(isBatch ? { ...payload, count: batchCount } : payload),
      });
      const jobs = data.jobs || (data.job ? [data.job] : []);
      if (!jobs.length) throw new Error('任务启动失败');
      state.currentJobId = jobs[0].id;
      state.refreshAccountIdAfterJob = refreshAccountId;
      state.jobs = jobs.concat(state.jobs.filter((job) => !jobs.some((item) => String(item.id) === String(job.id))));
      state.queue = data.queue || state.queue;
      if (!renderInPage) {
        const returnQuery = refreshAccountId ? `&returnAccount=${encodeURIComponent(refreshAccountId)}` : '';
        window.location.href = `/tasks?job=${encodeURIComponent(jobs[0].id)}${returnQuery}`;
        return;
      }
      renderQueueSummary();
      renderJobList();
      renderJob(currentJob());
      toast(isBatch ? `已启动 ${jobs.length} 个注册任务` : '任务已启动');
      startJobPolling();
    }

    function startJobPolling() {
      clearInterval(state.jobTimer);
      state.jobTimer = setInterval(() => loadJobBoard().catch((err) => toast(err.message)), 1100);
    }

    function renderJob(job) {
      const current = job || { status: 'idle', logs: [], result: {}, error: '', prompt: '' };
      const badge = $('jobState');
      badge.textContent = jobStatusLabel(current.status);
      badge.dataset.status = current.status || 'idle';
      updateCheckoutButton();
      if (current.status === 'waiting' && pageMode === 'tasks') openOpsModal();
      const lines = (current.logs || []).join('\n');
      const result = current.result && Object.keys(current.result).length ? `\n\n[结果] ${JSON.stringify(current.result, null, 2)}` : '';
      const error = current.error ? `\n\n[错误] ${current.error}` : '';
      $('jobLog').textContent = lines || '任务日志会显示在这里。';
      $('jobLog').textContent += result + error;
      $('jobLog').scrollTop = $('jobLog').scrollHeight;
      $('promptBox').classList.toggle('show', current.status === 'waiting');
      if (current.status === 'waiting') {
        $('promptInput').placeholder = current.prompt || '输入验证码后继续';
        $('promptInput').focus();
      } else {
        $('promptInput').placeholder = '输入邮箱验证码后继续';
      }
    }

    async function regenerateCheckout() {
      const accountId = Number($('accountId').value || 0);
      if (!accountId) return toast('请先选择账号');
      const email = $('email').value.trim();
      const password = $('password').value;
      if (!email || !password) return toast('当前账号缺少邮箱或密码，无法生成 checkout');
      $('opMode').value = 'login';
      $('opEmail').value = email;
      $('opPassword').value = password;
      closeAccountModal();
      await startJob({
        mode: 'login',
        email,
        password,
        account_id: accountId,
        generate_email: false,
        generate_password: false,
        create_checkout: true,
      }, accountId);
      toast('已开始重新生成 checkout');
    }

    async function refreshSession() {
      const accountId = Number($('accountId').value || 0);
      if (!accountId) return toast('请先选择账号');
      const email = $('email').value.trim();
      const password = $('password').value;
      if (!email || !password) return toast('当前账号缺少邮箱或密码，无法重新获取 Session');
      $('opMode').value = 'login';
      $('opEmail').value = email;
      $('opPassword').value = password;
      closeAccountModal();
      await startJob({
        mode: 'login',
        email,
        password,
        account_id: accountId,
        generate_email: false,
        generate_password: false,
        create_checkout: false,
      }, accountId);
      toast('已开始重新获取 Session');
    }

    async function submitPrompt() {
      if (!state.currentJobId) return toast('当前没有运行中的任务');
      const value = $('promptInput').value.trim();
      if (!value) return toast('验证码不能为空');
      const data = await request(`/api/ops/jobs/${state.currentJobId}/input`, {
        method: 'POST',
        body: JSON.stringify({ value }),
      });
      $('promptInput').value = '';
      state.queue = data.queue || state.queue;
      state.jobs = state.jobs.map((job) => String(job.id) === String(data.job.id) ? data.job : job);
      renderQueueSummary();
      renderJobList();
      renderJob(data.job);
      startJobPolling();
    }

    function reloadFromFirstPage() {
      state.page = 1;
      return loadAccounts().catch((err) => toast(err.message));
    }

    let searchTimer = null;
    $('search').addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(reloadFromFirstPage, 180);
    });
    $('planFilter').addEventListener('change', reloadFromFirstPage);
    $('statusFilter').addEventListener('change', reloadFromFirstPage);
    $('subscriptionFilter').addEventListener('change', reloadFromFirstPage);
    $('stockFilter').addEventListener('change', reloadFromFirstPage);
    $('pageSizeSelect').addEventListener('change', () => {
      state.pageSize = Number($('pageSizeSelect').value) || 50;
      reloadFromFirstPage();
    });
    $('prevPageBtn').addEventListener('click', () => {
      if (state.page <= 1) return;
      state.page -= 1;
      loadAccounts().catch((err) => toast(err.message));
    });
    $('nextPageBtn').addEventListener('click', () => {
      if (state.page >= state.totalPages) return;
      state.page += 1;
      loadAccounts().catch((err) => toast(err.message));
    });
    $('opMode').addEventListener('change', syncOperationControls);
    $('accountForm').addEventListener('submit', (event) => event.preventDefault());
    $('runBtn').addEventListener('click', (event) => {
      event.preventDefault();
      window.location.href = '/tasks';
    });
    $('accountModalCloseBtn').addEventListener('click', closeAccountModal);
    $('opsModalCloseBtn').addEventListener('click', closeOpsModal);
    $('accountModalBackdrop').addEventListener('click', (event) => {
      if (event.target === $('accountModalBackdrop')) closeAccountModal();
    });
    $('opsModalBackdrop').addEventListener('click', (event) => {
      if (event.target === $('opsModalBackdrop')) closeOpsModal();
    });
    $('confirmOkBtn').addEventListener('click', () => closeConfirm(true));
    $('confirmCancelBtn').addEventListener('click', () => closeConfirm(false));
    $('confirmBackdrop').addEventListener('click', (event) => {
      if (event.target === $('confirmBackdrop')) closeConfirm(false);
    });
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && $('confirmBackdrop').classList.contains('is-open')) {
        event.preventDefault();
        closeConfirm(true);
        return;
      }
      if (event.key !== 'Escape') return;
      if ($('confirmBackdrop').classList.contains('is-open')) { closeConfirm(false); return; }
      if ($('accountModalBackdrop').classList.contains('is-open')) closeAccountModal();
      if ($('opsModalBackdrop').classList.contains('is-open')) closeOpsModal();
    });
    $('deleteBtn').addEventListener('click', () => deleteSelected().catch((err) => toast(err.message)));
    $('stockToggleBtn').addEventListener('click', () => toggleStockStatus().catch((err) => toast(err.message)));
    $('markSubscribedBtn').addEventListener('click', () => markSubscribed().catch((err) => toast(err.message)));
    $('abandonBtn').addEventListener('click', () => markAbandoned().catch((err) => toast(err.message)));
    $('selectAllVisible').addEventListener('change', () => toggleSelectAllVisible($('selectAllVisible').checked));
    $('batchStockOutBtn').addEventListener('click', () => batchUpdateStockStatus(STOCK_OUT).catch((err) => toast(err.message)));
    $('batchStockInBtn').addEventListener('click', () => batchUpdateStockStatus(STOCK_IN).catch((err) => toast(err.message)));
    $('batchRefreshPlanBtn').addEventListener('click', () => batchRefreshSubscriptionTypes().catch((err) => toast(err.message)));
    $('batchExportJsonlBtn').addEventListener('click', () => batchExportSessionJsonl().catch((err) => toast(err.message)));
    $('batchExportAirgateBtn').addEventListener('click', () => batchExportAirgateJson().catch((err) => toast(err.message)));
    $('batchDeleteBtn').addEventListener('click', () => batchDeleteAccounts().catch((err) => toast(err.message)));
    $('batchClearBtn').addEventListener('click', clearSelection);
    $('copyLineBtn').addEventListener('click', () => copyText(compactLine(), '账号行已复制'));
    $('refreshPlanBtn').addEventListener('click', () => refreshSubscriptionType().catch((err) => toast(err.message)));
    $('regenCheckoutBtn').addEventListener('click', () => regenerateCheckout().catch((err) => toast(err.message)));
    $('refreshSessionBtn').addEventListener('click', () => refreshSession().catch((err) => toast(err.message)));
    $('startJobBtn').addEventListener('click', () => startJob().catch((err) => toast(err.message)));
    $('autoSaveBtn').addEventListener('click', () => saveAutoRegisterConfig().catch((err) => toast(err.message)));
    $('autoStartBtn').addEventListener('click', () => startAutoRegister().catch((err) => toast(err.message)));
    $('autoStopBtn').addEventListener('click', () => stopAutoRegister().catch((err) => toast(err.message)));
    $('airgateSaveBtn').addEventListener('click', () => saveAirgateMonitorConfig().catch((err) => toast(err.message)));
    $('airgateStartBtn').addEventListener('click', () => startAirgateMonitor().catch((err) => toast(err.message)));
    $('airgateStopBtn').addEventListener('click', () => stopAirgateMonitor().catch((err) => toast(err.message)));
    $('airgateRunOnceBtn').addEventListener('click', () => runAirgateMonitorOnce().catch((err) => toast(err.message)));
    $('submitPromptBtn').addEventListener('click', () => submitPrompt().catch((err) => toast(err.message)));
    $('promptInput').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') submitPrompt().catch((err) => toast(err.message));
    });
    $('exportBtn').addEventListener('click', () => exportAccountsTxt().catch((err) => toast(err.message)));
    $('exportTokensBtn').addEventListener('click', () => exportTokensJsonl().catch((err) => toast(err.message)));
    $('exportCheckoutsBtn').addEventListener('click', () => exportCheckoutsJsonl().catch((err) => toast(err.message)));
    $('exportAirgateBtn').addEventListener('click', () => exportAirgateJson().catch((err) => toast(err.message)));
    $('logoutBtn').addEventListener('click', async () => {
      try {
        await request('/api/auth/logout', { method: 'POST' });
      } catch (err) {
        // 即使会话已经过期，也直接回到登录页。
      }
      window.location.href = '/login';
    });

    let confirmResolve = null;
    function showConfirm(message, options = {}) {
      const { title = '确认操作', okText = '确认', cancelText = '取消', danger = false } = options;
      $('confirmTitle').textContent = title;
      $('confirmMessage').textContent = message;
      const okBtn = $('confirmOkBtn');
      okBtn.textContent = okText;
      okBtn.className = danger ? 'danger' : 'accent';
      $('confirmCancelBtn').textContent = cancelText;
      $('confirmBackdrop').classList.add('is-open');
      setTimeout(() => okBtn.focus(), 30);
      return new Promise((resolve) => { confirmResolve = resolve; });
    }
    function closeConfirm(result) {
      if (!$('confirmBackdrop').classList.contains('is-open')) return;
      $('confirmBackdrop').classList.remove('is-open');
      if (confirmResolve) {
        const resolve = confirmResolve;
        confirmResolve = null;
        resolve(result);
      }
    }

    async function flashCopied(node) {
      node.classList.add('copied');
      setTimeout(() => node.classList.remove('copied'), 700);
    }

    document.addEventListener('click', async (event) => {
      const node = event.target;
      if (!(node instanceof HTMLElement) || !node.classList.contains('copy-on-click')) return;
      const value = (node.value || '').trim();
      if (!hasValue(value)) return toast('没有可复制内容');
      try {
        await clipboardWrite(value);
        toast('已复制');
        flashCopied(node);
      } catch (err) {
        toast(err.message || '复制失败');
      }
    });

    document.addEventListener('click', async (event) => {
      const node = event.target;
      if (!(node instanceof HTMLElement) || !node.hasAttribute('data-copy-checkout')) return;
      const value = (node.getAttribute('data-copy-checkout') || '').trim();
      if (!hasValue(value)) return toast('没有可复制内容');
      try {
        await clipboardWrite(value);
        toast('Checkout 链接已复制，请用无痕浏览器打开');
        flashCopied(node);
      } catch (err) {
        toast(err.message || '复制失败');
      }
    });

    $('copySessionBtn').addEventListener('click', async () => {
      const value = ($('sessionJson').value || '').trim();
      if (!hasValue(value)) return toast('Session 为空');
      try {
        await clipboardWrite(value);
        toast('Session JSON 已复制');
        flashCopied($('copySessionBtn'));
      } catch (err) {
        toast(err.message || '复制失败');
      }
    });

    async function initApp() {
      setupPageChrome();
      await loadCurrentUser();
      if (pageMode === 'tasks' || pageMode === 'settings') {
        state.returnAccountId = new URLSearchParams(window.location.search).get('returnAccount') || '';
        syncOperationControls();
        await loadJobBoard();
      } else {
        resetForm();
        await loadAccounts();
        startAccountsPolling();
      }
    }
    initApp().catch((err) => toast(err.message));
  </script>
</body>
</html>
"""


LOGIN_PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Protocol Reg 登录平台</title>
  <style>
    :root {
      --ink: #11100d;
      --paper: #f5efe2;
      --line: rgba(17, 16, 13, .16);
      --muted: #6f6659;
      --accent: #d8612c;
      --accent-2: #0f6b5f;
      --bad: #a33b2f;
      --shadow: 0 24px 80px rgba(48, 39, 25, .18);
      --mono: "JetBrains Mono", "Cascadia Code", Consolas, monospace;
      --body: "Aptos", "Gill Sans", "Trebuchet MS", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      color: var(--ink);
      font-family: var(--body);
      background: linear-gradient(135deg, #f8f0df 0%, #efe4d2 52%, #e3d3bd 100%);
    }
    .login {
      width: min(420px, calc(100vw - 28px));
      border: 1px solid var(--line);
      background: rgba(245, 239, 226, .82);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      border-radius: 22px;
      padding: 24px;
    }
    .eyebrow {
      font: 11px/1.2 var(--mono);
      color: var(--muted);
      letter-spacing: .16em;
      text-transform: uppercase;
    }
    h1 {
      margin: 8px 0 22px;
      font-size: 28px;
      line-height: 1.15;
    }
    label {
      display: block;
      color: var(--muted);
      font: 12px/1.2 var(--mono);
      margin: 14px 0 8px;
    }
    input {
      width: 100%;
      border: 1px solid rgba(17,16,13,.14);
      background: rgba(255,255,255,.55);
      color: var(--ink);
      border-radius: 12px;
      padding: 12px 13px;
      font: 15px/1.2 inherit;
      outline: none;
    }
    input:focus {
      border-color: rgba(216,97,44,.72);
      box-shadow: 0 0 0 4px rgba(216,97,44,.12);
    }
    button {
      width: 100%;
      margin-top: 18px;
      border: 0;
      border-radius: 12px;
      padding: 12px 14px;
      color: #fff;
      background: var(--accent);
      font-weight: 700;
      cursor: pointer;
    }
    .msg {
      min-height: 22px;
      margin-top: 12px;
      color: var(--bad);
      font-size: 13px;
    }
    .hint {
      margin-top: 18px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
    }
  </style>
</head>
<body>
  <main class="login">
    <div class="eyebrow">Protocol Reg</div>
    <h1>平台登录</h1>
    <form id="loginForm">
      <label for="username">用户名</label>
      <input id="username" autocomplete="username" value="admin" />
      <label for="password">密码</label>
      <input id="password" type="password" autocomplete="current-password" />
      <button id="loginBtn" type="submit">登录</button>
      <div class="msg" id="message"></div>
    </form>
    <div class="hint">第一次启动时，终端会打印初始管理员密码。</div>
  </main>
  <script>
    const form = document.getElementById('loginForm');
    const message = document.getElementById('message');
    const button = document.getElementById('loginBtn');
    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      message.textContent = '';
      button.disabled = true;
      try {
        const response = await fetch('/api/auth/login', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({
            username: document.getElementById('username').value.trim(),
            password: document.getElementById('password').value,
          }),
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || '登录失败');
        const user = data.user || {};
        window.location.href = user.role === 'operator' ? '/operator' : '/';
      } catch (err) {
        message.textContent = err.message || '登录失败';
      } finally {
        button.disabled = false;
      }
    });
  </script>
</body>
</html>
"""


ADMIN_USERS_PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Protocol Reg 用户管理</title>
  <style>
    :root {
      --ink: #11100d;
      --paper: #f5efe2;
      --paper-2: #ebe1cf;
      --line: rgba(17, 16, 13, .16);
      --muted: #6f6659;
      --accent: #d8612c;
      --accent-2: #0f6b5f;
      --good: #2d7a46;
      --bad: #a33b2f;
      --shadow: 0 24px 80px rgba(48, 39, 25, .16);
      --mono: "JetBrains Mono", "Cascadia Code", Consolas, monospace;
      --display: "Fraunces", "Iowan Old Style", Georgia, serif;
      --body: "Aptos", "Gill Sans", "Trebuchet MS", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: var(--body);
      background:
        radial-gradient(circle at 20% 10%, rgba(216, 97, 44, .22), transparent 30%),
        radial-gradient(circle at 95% 0%, rgba(15, 107, 95, .18), transparent 28%),
        linear-gradient(135deg, #f8f0df 0%, #efe4d2 52%, #e3d3bd 100%);
      overflow-x: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: .35;
      background-image:
        linear-gradient(rgba(17,16,13,.045) 1px, transparent 1px),
        linear-gradient(90deg, rgba(17,16,13,.045) 1px, transparent 1px);
      background-size: 26px 26px;
      mask-image: linear-gradient(to bottom, black, transparent 85%);
    }
    .shell { width: min(1440px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 36px; position: relative; }
    .topbar, .panel {
      border: 1px solid var(--line);
      background: rgba(245, 239, 226, .78);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      border-radius: 20px;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 12px 20px;
      margin-bottom: 16px;
      overflow: hidden;
      position: relative;
      border-radius: 22px;
      animation: rise .45s ease both;
    }
    .topbar::after {
      content: "";
      position: absolute;
      width: 120px;
      height: 120px;
      right: -50px;
      top: -55px;
      border-radius: 50%;
      border: 22px solid rgba(216, 97, 44, .14);
      pointer-events: none;
    }
    .brand {
      display: flex;
      flex-direction: column;
      gap: 3px;
      flex: 0 0 auto;
      position: relative;
      z-index: 1;
    }
    .brand-eyebrow {
      font-family: var(--mono);
      font-size: 10px;
      letter-spacing: .18em;
      color: var(--accent-2);
      text-transform: uppercase;
    }
    .brand-mark {
      font-family: var(--display);
      font-size: 24px;
      line-height: 1;
      letter-spacing: 0;
    }
    .top-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      flex: 1 1 auto;
      position: relative;
      z-index: 1;
    }
    .page-links {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .nav-main {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 12px;
      flex-wrap: wrap;
    }
    .page-nav {
      min-height: 42px;
      padding-inline: 16px;
      border-radius: 12px;
    }
    .page-nav.is-active {
      background: var(--ink);
      color: #fff;
      box-shadow: 0 12px 28px rgba(17,16,13,.18);
    }
    a, button {
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 999px;
      min-height: 36px;
      padding: 8px 12px;
      background: rgba(255,255,255,.38);
      color: var(--ink);
      text-decoration: none;
      font: 13px/1.2 inherit;
      cursor: pointer;
      white-space: nowrap;
      transition: background .16s ease, border-color .16s ease, color .16s ease, transform .16s ease;
    }
    button:hover:not(:disabled), a:hover { transform: translateY(-1px); }
    button:disabled {
      opacity: .42;
      cursor: not-allowed;
      transform: none;
    }
    button.primary { color: #fff; background: var(--accent); border-color: var(--accent); }
    button.danger { color: #fff; background: var(--bad); border-color: var(--bad); }
    .badge {
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 999px;
      padding: 8px 10px;
      color: var(--muted);
      font: 12px/1 var(--mono);
    }
    .stats {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 16px;
    }
    .stat-card {
      border: 1px solid rgba(17,16,13,.1);
      border-radius: 14px;
      padding: 10px 12px;
      min-width: 120px;
      background: rgba(255,255,255,.34);
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .stat-label {
      color: var(--muted);
      font: 11px/1.1 var(--mono);
    }
    .stat-value {
      font-weight: 800;
      font-size: 18px;
      line-height: 1.1;
      font-family: var(--mono);
      letter-spacing: 0;
      font-variant-numeric: tabular-nums;
    }
    .section-head {
      display: flex;
      align-items: end;
      justify-content: space-between;
      gap: 12px;
      margin: 16px 0 10px;
    }
    .section-title {
      color: var(--ink);
      font-size: 16px;
      font-weight: 800;
    }
    .section-subtitle {
      color: var(--muted);
      font: 12px/1.2 var(--mono);
      margin-top: 3px;
    }
    .operator-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 10px;
      margin-bottom: 16px;
    }
    .operator-card {
      border: 1px solid rgba(17,16,13,.1);
      border-radius: 14px;
      background: rgba(255,255,255,.34);
      padding: 12px;
      display: grid;
      gap: 10px;
    }
    .operator-card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 8px;
    }
    .operator-name {
      font-weight: 800;
      font-size: 15px;
    }
    .operator-role {
      color: var(--muted);
      font: 11px/1.2 var(--mono);
      margin-top: 4px;
    }
    .operator-metrics {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
    }
    .operator-metric {
      border-radius: 12px;
      background: rgba(248,241,229,.84);
      border: 1px solid rgba(17,16,13,.08);
      padding: 8px 10px;
    }
    .operator-metric-label {
      color: var(--muted);
      font: 11px/1.1 var(--mono);
    }
    .operator-metric-value {
      margin-top: 4px;
      font-family: var(--mono);
      font-size: 16px;
      font-weight: 800;
      line-height: 1;
      font-variant-numeric: tabular-nums;
    }
    .operator-foot {
      display: flex;
      justify-content: space-between;
      gap: 8px;
      align-items: center;
      color: var(--muted);
      font: 12px/1.2 var(--mono);
    }
    .empty {
      padding: 18px 14px;
      color: var(--muted);
      text-align: center;
      border: 1px dashed rgba(17,16,13,.14);
      border-radius: 14px;
      background: rgba(255,255,255,.28);
    }
    .panel { padding: 16px; }
    .grid {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 10px;
      align-items: end;
      margin-bottom: 16px;
    }
    label { display: grid; gap: 6px; color: var(--muted); font: 12px/1.2 var(--mono); }
    input:not([type="checkbox"]), select {
      width: 100%;
      border: 1px solid rgba(17,16,13,.14);
      background: rgba(255,255,255,.55);
      color: var(--ink);
      border-radius: 12px;
      padding: 10px 11px;
      font: 14px/1.2 var(--body);
      outline: none;
    }
    .span-2 { grid-column: span 2; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; }
    .permission-field {
      grid-column: 1 / -1;
      display: grid;
      gap: 8px;
      color: var(--muted);
      font: 12px/1.2 var(--mono);
    }
    .permission-editor {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .permission-summary {
      min-height: 34px;
      display: flex;
      align-items: center;
    }
    .permission-options {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
    }
    .permission-option {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 12px;
      padding: 9px 10px;
      background: rgba(255,255,255,.4);
      color: var(--ink);
      font: 13px/1.2 var(--body);
      cursor: pointer;
    }
    .permission-option input {
      width: 14px;
      height: 14px;
      margin: 0;
      accent-color: var(--accent);
    }
    .permission-option input:disabled + span {
      color: var(--muted);
    }
    .permission-tags {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      max-width: 540px;
    }
    .permission-tag {
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 999px;
      padding: 5px 8px;
      background: rgba(255,255,255,.42);
      color: var(--ink);
      font: 12px/1.1 var(--body);
      white-space: nowrap;
    }
    .permission-tag.all {
      color: var(--accent-2);
      background: rgba(15,107,95,.1);
    }
    .muted { color: var(--muted); }
    .modal-backdrop {
      position: fixed;
      inset: 0;
      display: grid;
      place-items: center;
      padding: 20px;
      background: rgba(17,16,13,.34);
      z-index: 20;
    }
    #permissionModal {
      z-index: 30;
    }
    .modal-backdrop.hidden { display: none; }
    .modal {
      width: min(520px, calc(100vw - 40px));
      border: 1px solid var(--line);
      border-radius: 18px;
      background: #f5efe2;
      box-shadow: var(--shadow);
      padding: 16px;
    }
    .modal-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }
    .modal-head h2 {
      margin: 0;
      font-size: 20px;
    }
    .modal-user {
      width: min(760px, calc(100vw - 40px));
    }
    .modal-close {
      width: 34px;
      height: 34px;
      padding: 0;
      border-radius: 999px;
      font-size: 18px;
      line-height: 1;
    }
    .modal-actions {
      display: flex;
      justify-content: flex-end;
      gap: 8px;
      margin-top: 16px;
    }
    table { width: 100%; border-collapse: collapse; overflow: hidden; border-radius: 14px; }
    th, td {
      text-align: left;
      padding: 11px 10px;
      border-bottom: 1px solid rgba(17,16,13,.1);
      vertical-align: top;
      font-size: 13px;
    }
    th { color: var(--muted); font: 11px/1.2 var(--mono); background: rgba(255,255,255,.26); }
    td code { font-family: var(--mono); font-size: 12px; word-break: break-all; }
    .status-active { color: var(--good); }
    .status-disabled { color: var(--bad); }
    .toast {
      position: fixed;
      right: 22px;
      bottom: 22px;
      opacity: 0;
      transform: translateY(10px);
      color: #fff;
      background: var(--ink);
      border-radius: 14px;
      padding: 12px 14px;
      transition: .18s ease;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
    @keyframes rise {
      from { opacity: 0; transform: translateY(18px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @media (max-width: 860px) {
      .grid { grid-template-columns: 1fr; }
      .span-2 { grid-column: auto; }
      .permission-options { grid-template-columns: 1fr; }
      .operator-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .topbar { align-items: stretch; }
      .top-actions { justify-content: flex-start; }
      .nav-main { justify-content: flex-start; width: 100%; }
      table { display: block; overflow-x: auto; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-eyebrow">Protocol Reg</div>
        <div class="brand-mark" id="brandMark">账号库控制台</div>
      </div>
      <div class="top-actions">
        <nav class="page-links" id="pageLinks" aria-label="页面导航"></nav>
        <div class="nav-main">
          <span class="badge" id="currentUser">加载中</span>
          <button id="logoutBtn" type="button">退出</button>
        </div>
      </div>
    </header>
    <section class="panel">
      <div class="stats" id="userStats"></div>
      <div class="section-head">
        <div>
          <div class="section-title">订阅操作员概览</div>
          <div class="section-subtitle">按领取、点击、确认、失败记录统计</div>
        </div>
      </div>
      <div class="operator-grid" id="operatorStats"></div>
      <div class="toolbar" style="display:flex;justify-content:flex-end;margin-bottom:16px;">
        <button class="primary" id="newUserBtn" type="button">新增操作员</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>用户名</th>
            <th>角色</th>
            <th>状态</th>
            <th>最近登录</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody id="userRows"></tbody>
      </table>
    </section>
  </main>
  <div class="modal-backdrop hidden" id="userModal" role="dialog" aria-modal="true" aria-labelledby="userModalTitle">
    <div class="modal modal-user">
      <div class="modal-head">
        <h2 id="userModalTitle">新增操作员</h2>
        <button class="modal-close" id="userModalClose" type="button" aria-label="关闭">×</button>
      </div>
      <input type="hidden" id="userId" />
      <div class="grid">
        <label>用户名<input id="username" placeholder="operator01" autocomplete="off" /></label>
        <label>显示名<input id="displayName" placeholder="操作员姓名" autocomplete="off" /></label>
        <label>初始密码<input id="password" type="password" placeholder="新建时必填" autocomplete="new-password" /></label>
        <label>状态
          <select id="status">
            <option value="active">启用</option>
            <option value="disabled">禁用</option>
          </select>
        </label>
        <div class="permission-field" style="display:none">
          <div>权限</div>
          <div class="permission-editor">
            <div class="permission-summary" id="permissionSummary"></div>
            <button id="openPermissionModalBtn" type="button">编辑权限</button>
          </div>
        </div>
      </div>
      <div class="modal-actions">
        <button id="cancelBtn" type="button">取消</button>
        <button class="primary" id="saveBtn" type="button">保存用户</button>
      </div>
    </div>
  </div>
  <div class="modal-backdrop hidden" id="permissionModal" role="dialog" aria-modal="true" aria-labelledby="permissionModalTitle">
    <div class="modal">
      <div class="modal-head">
        <h2 id="permissionModalTitle">编辑权限</h2>
        <button class="modal-close" id="permissionModalClose" type="button" aria-label="关闭">×</button>
      </div>
      <div class="permission-options" id="permissionModalOptions"></div>
      <div class="modal-actions">
        <button id="permissionModalCancel" type="button">取消</button>
        <button class="primary" id="permissionModalSave" type="button">保存权限</button>
      </div>
    </div>
  </div>
  <div class="toast" id="toast"></div>
  <script>
    const $ = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
    const PERMISSIONS = [
      ['view_subscription_accounts', '查看订阅账号'],
      ['claim_subscription_account', '领取订阅任务'],
      ['mark_subscription_done', '标记已订阅'],
      ['mark_subscription_failed', '标记失败'],
    ];
    const DEFAULT_PERMISSIONS = PERMISSIONS.map((item) => item[0]);
    let users = [];
    let overviewStats = {};
    let currentUserId = '';
    let selectedPermissions = [...DEFAULT_PERMISSIONS];
    let formRole = 'operator';
    let permissionModalContext = { type: 'form', userId: '' };
    function roleLabel(role) {
      return String(role || '').toLowerCase() === 'admin' ? '管理员' : '操作员';
    }
    function statusLabel(status) {
      return String(status || '').toLowerCase() === 'disabled' ? '禁用' : '启用';
    }
    function toast(message) {
      const node = $('toast');
      node.textContent = message;
      node.classList.add('show');
      clearTimeout(window.__toastTimer);
      window.__toastTimer = setTimeout(() => node.classList.remove('show'), 2200);
    }
    async function request(url, options = {}) {
      const response = await fetch(url, { headers: { 'content-type': 'application/json' }, ...options });
      if (response.status === 401) {
        window.location.href = '/login';
        throw new Error('未登录或会话已过期');
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '请求失败');
      return data;
    }
    function renderPermissionModalOptions(values, disabled = false) {
      const selected = new Set(Array.isArray(values) ? values : DEFAULT_PERMISSIONS);
      const allSelected = selected.has('*');
      $('permissionModalOptions').innerHTML = PERMISSIONS.map(([key, label]) => `
        <label class="permission-option" title="${escapeHtml(key)}">
          <input type="checkbox" data-modal-permission="${escapeHtml(key)}" ${allSelected || selected.has(key) ? 'checked' : ''} ${disabled ? 'disabled' : ''} />
          <span>${escapeHtml(label)}</span>
        </label>
      `).join('');
    }
    function setPermissions(values) {
      selectedPermissions = Array.isArray(values) ? [...values] : [...DEFAULT_PERMISSIONS];
      renderPermissionSummary();
    }
    function renderPermissionSummary() {
      $('permissionSummary').innerHTML = permissionTags(selectedPermissions);
      $('openPermissionModalBtn').disabled = formRole === 'admin';
    }
    function permissionsArray() {
      return [...selectedPermissions];
    }
    function modalPermissionsArray() {
      return Array.from(document.querySelectorAll('[data-modal-permission]:checked'))
        .map((input) => input.dataset.modalPermission)
        .filter(Boolean);
    }
    function permissionLabel(key) {
      const match = PERMISSIONS.find((item) => item[0] === key);
      return match ? match[1] : key;
    }
    function permissionTags(permissions) {
      const values = Array.isArray(permissions) ? permissions : [];
      if (values.includes('*')) {
        return '<div class="permission-tags"><span class="permission-tag all" title="*">全部权限</span></div>';
      }
      const tags = values
        .filter((key) => key && key !== '*')
        .map((key) => `<span class="permission-tag" title="${escapeHtml(key)}">${escapeHtml(permissionLabel(key))}</span>`)
        .join('');
      return `<div class="permission-tags">${tags || '<span class="muted">未分配</span>'}</div>`;
    }
    function formatDate(value) {
      if (!value || value === 'null') return '无记录';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString('zh-CN', { hour12: false });
    }
    function renderPageLinks(user) {
      const role = String(user?.role || '').toLowerCase();
      const links = role === 'admin'
        ? [
            ['/', '账号管理'],
            ['/tasks', '任务页面'],
            ['/settings', '设置'],
            ['/operator', '订阅处理'],
            ['/admin/users', '用户管理'],
          ]
        : [
            ['/operator', '订阅处理'],
          ];
      $('pageLinks').innerHTML = links.map(([href, label]) => `
        <a class="page-nav${href === window.location.pathname ? ' is-active' : ''}" href="${escapeHtml(href)}">${escapeHtml(label)}</a>
      `).join('');
    }
    async function loadCurrentUser() {
      const data = await request('/api/auth/me');
      const user = data.user || {};
      currentUserId = String(user.id || '');
      $('currentUser').textContent = `${user.username || '未知'} · ${roleLabel(user.role)}`;
      renderPageLinks(user);
      return user;
    }
    async function loadUsers() {
      const [usersData, overviewData] = await Promise.all([
        request('/api/admin/users'),
        request('/api/accounts?search=&status=all&plan=all&stock_status=all&subscription_status=all&page=1&page_size=1'),
      ]);
      users = usersData.items || [];
      overviewStats = overviewData.stats || {};
      renderOverview();
      renderOperatorStats();
      renderUsers();
    }
    function renderOverview() {
      const subscription = overviewStats.subscription_statuses || {};
      const operatorStats = Array.isArray(overviewStats.operator_stats) ? overviewStats.operator_stats : [];
      const activeOperators = users.filter((user) => user.role === 'operator' && user.status !== 'disabled').length;
      const enabledAdmins = users.filter((user) => user.role === 'admin' && user.status !== 'disabled').length;
      const disabledUsers = users.filter((user) => user.status === 'disabled').length;
      const cards = [
        ['账号总数', overviewStats.total ?? 0],
        ['待订阅', subscription['待订阅'] ?? 0],
        ['待核实', subscription['已点击订阅'] ?? 0],
        ['已确认', subscription['已确认订阅'] ?? 0],
        ['活跃操作员', activeOperators],
        ['启用管理员', enabledAdmins],
        ['停用用户', disabledUsers],
        ['有领取记录', operatorStats.length],
      ];
      $('userStats').innerHTML = cards.map(([label, value]) => `
        <div class="stat-card">
          <div class="stat-label">${escapeHtml(label)}</div>
          <div class="stat-value">${escapeHtml(value)}</div>
        </div>
      `).join('');
    }
    function renderOperatorStats() {
      const operatorStats = Array.isArray(overviewStats.operator_stats) ? overviewStats.operator_stats : [];
      if (!operatorStats.length) {
        $('operatorStats').innerHTML = '<div class="empty">暂无订阅操作记录。</div>';
        return;
      }
      $('operatorStats').innerHTML = operatorStats.map((row) => {
        const name = row.display_name || row.username || `#${row.operator_id}`;
        return `
          <article class="operator-card">
            <div class="operator-card-head">
              <div>
                <div class="operator-name">${escapeHtml(name)}</div>
                <div class="operator-role">${escapeHtml(row.username || `#${row.operator_id}`)} · ${escapeHtml(roleLabel(row.role))}</div>
              </div>
              <span class="badge">${escapeHtml(row.total || 0)} 单</span>
            </div>
            <div class="operator-metrics">
              <div class="operator-metric">
                <div class="operator-metric-label">待订阅</div>
                <div class="operator-metric-value">${escapeHtml(row.pending_count || 0)}</div>
              </div>
              <div class="operator-metric">
                <div class="operator-metric-label">处理中</div>
                <div class="operator-metric-value">${escapeHtml(row.claimed_count || 0)}</div>
              </div>
              <div class="operator-metric">
                <div class="operator-metric-label">待核实</div>
                <div class="operator-metric-value">${escapeHtml(row.marked_count || 0)}</div>
              </div>
              <div class="operator-metric">
                <div class="operator-metric-label">已确认</div>
                <div class="operator-metric-value">${escapeHtml(row.verified_count || 0)}</div>
              </div>
              <div class="operator-metric">
                <div class="operator-metric-label">失败</div>
                <div class="operator-metric-value">${escapeHtml(row.failed_count || 0)}</div>
              </div>
            </div>
            <div class="operator-foot">
              <span>最近活动</span>
              <strong>${escapeHtml(formatDate(row.last_activity_at))}</strong>
            </div>
          </article>
        `;
      }).join('');
    }
    function renderUsers() {
      $('userRows').innerHTML = users.map((user) => {
        const statusClass = user.status === 'disabled' ? 'status-disabled' : 'status-active';
        const canDelete = String(user.id) !== currentUserId;
        const actions = `
          <button type="button" data-edit="${escapeHtml(user.id)}">编辑</button>
          <button type="button" data-reset="${escapeHtml(user.id)}">重置密码</button>
          ${canDelete ? `<button class="danger" type="button" data-delete="${escapeHtml(user.id)}">删除</button>` : '<span class="muted">当前账号</span>'}
        `;
        return `
          <tr>
            <td>${escapeHtml(user.id)}</td>
            <td><strong>${escapeHtml(user.username)}</strong><br>${escapeHtml(user.display_name || '')}</td>
            <td>${escapeHtml(roleLabel(user.role))}</td>
            <td class="${statusClass}">${escapeHtml(statusLabel(user.status))}</td>
            <td>${escapeHtml(formatDate(user.last_login_at))}</td>
            <td>${actions}</td>
          </tr>
        `;
      }).join('') || '<tr><td colspan="6">暂无用户</td></tr>';
      document.querySelectorAll('[data-edit]').forEach((button) => {
        button.addEventListener('click', () => editUser(button.dataset.edit));
      });
      document.querySelectorAll('[data-reset]').forEach((button) => {
        button.addEventListener('click', () => resetPassword(button.dataset.reset).catch((err) => toast(err.message)));
      });
      document.querySelectorAll('[data-delete]').forEach((button) => {
        button.addEventListener('click', () => deleteUser(button.dataset.delete).catch((err) => toast(err.message)));
      });
    }
    function clearForm() {
      formRole = 'operator';
      $('userId').value = '';
      $('username').value = '';
      $('username').disabled = false;
      $('displayName').value = '';
      $('password').value = '';
      $('status').value = 'active';
      setPermissions(DEFAULT_PERMISSIONS);
      $('saveBtn').textContent = '创建操作员';
      $('userModalTitle').textContent = '新增操作员';
      $('permissionModalSave').disabled = false;
    }
    function openUserModalForCreate() {
      closePermissionModal();
      clearForm();
      $('userModal').classList.remove('hidden');
      setTimeout(() => $('username').focus(), 0);
    }
    function closeUserModal() {
      closePermissionModal();
      $('userModal').classList.add('hidden');
      clearForm();
    }
    function editUser(id) {
      const user = users.find((item) => String(item.id) === String(id));
      if (!user) return;
      closePermissionModal();
      clearForm();
      $('userId').value = user.id;
      $('username').value = user.username;
      $('username').disabled = true;
      $('displayName').value = user.display_name || '';
      $('password').value = '';
      $('status').value = user.status || 'active';
      formRole = user.role || 'operator';
      setPermissions(user.permissions || (formRole === 'admin' ? ['*'] : DEFAULT_PERMISSIONS));
      $('saveBtn').textContent = '保存修改';
      $('userModalTitle').textContent = '编辑用户';
      $('userModal').classList.remove('hidden');
      setTimeout(() => $('displayName').focus(), 0);
    }
    function openPermissionModalForForm() {
      if (formRole === 'admin') {
        toast('管理员默认拥有全部权限');
        return;
      }
      permissionModalContext = { type: 'form', userId: '' };
      $('permissionModalTitle').textContent = '编辑权限';
      $('permissionModalSave').disabled = false;
      renderPermissionModalOptions(selectedPermissions);
      $('permissionModal').classList.remove('hidden');
    }
    function openPermissionModalForUser(id) {
      const user = users.find((item) => String(item.id) === String(id));
      if (!user) return;
      if (user.role === 'admin') {
        toast('管理员默认拥有全部权限');
        return;
      }
      permissionModalContext = { type: 'user', userId: String(user.id) };
      $('permissionModalTitle').textContent = `${user.username} · 权限`;
      $('permissionModalSave').disabled = false;
      renderPermissionModalOptions(user.permissions || DEFAULT_PERMISSIONS);
      $('permissionModal').classList.remove('hidden');
    }
    function closePermissionModal() {
      $('permissionModal').classList.add('hidden');
      permissionModalContext = { type: 'form', userId: '' };
    }
    function closeTopModalOnEscape(event) {
      if (event.key !== 'Escape') return;
      if (!$('permissionModal').classList.contains('hidden')) {
        closePermissionModal();
        return;
      }
      if (!$('userModal').classList.contains('hidden')) {
        closeUserModal();
      }
    }
    async function savePermissionModal() {
      const permissions = modalPermissionsArray();
      if (permissionModalContext.type === 'form') {
        setPermissions(permissions);
        closePermissionModal();
        return;
      }
      const id = permissionModalContext.userId;
      if (!id) return;
      $('permissionModalSave').disabled = true;
      await request(`/api/admin/users/${encodeURIComponent(id)}`, {
        method: 'PATCH',
        body: JSON.stringify({ permissions }),
      });
      toast('权限已更新');
      closePermissionModal();
      await loadUsers();
    }
    async function saveUser() {
      const id = $('userId').value.trim();
      const payload = {
        display_name: $('displayName').value.trim(),
        permissions: permissionsArray(),
        status: $('status').value,
      };
      if (!id) {
        payload.username = $('username').value.trim();
        payload.password = $('password').value;
        if (!payload.username) return toast('用户名不能为空');
        if (!payload.password) return toast('新建操作员需要初始密码');
        await request('/api/admin/users', { method: 'POST', body: JSON.stringify(payload) });
        toast('操作员已创建');
      } else {
        await request(`/api/admin/users/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(payload) });
        toast('用户已更新');
      }
      closeUserModal();
      await loadUsers();
    }
    async function resetPassword(id) {
      const password = window.prompt('输入新密码（至少 6 位）');
      if (!password) return;
      await request(`/api/admin/users/${encodeURIComponent(id)}/reset-password`, {
        method: 'POST',
        body: JSON.stringify({ password, must_change_password: false }),
      });
      toast('密码已重置');
    }
    async function deleteUser(id) {
      const user = users.find((item) => String(item.id) === String(id));
      if (!user) return;
      const confirmed = window.confirm(`确定删除用户 ${user.username} 吗？\n该用户的会话会失效，处理中账号会自动回到待订阅。`);
      if (!confirmed) return;
      await request(`/api/admin/users/${encodeURIComponent(id)}`, { method: 'DELETE' });
      toast('用户已删除');
      await loadUsers();
    }
    $('newUserBtn').addEventListener('click', openUserModalForCreate);
    $('userModalClose').addEventListener('click', closeUserModal);
    $('cancelBtn').addEventListener('click', closeUserModal);
    $('userModal').addEventListener('click', (event) => {
      if (event.target === $('userModal')) closeUserModal();
    });
    $('openPermissionModalBtn').addEventListener('click', openPermissionModalForForm);
    $('permissionModalClose').addEventListener('click', closePermissionModal);
    $('permissionModalCancel').addEventListener('click', closePermissionModal);
    $('permissionModal').addEventListener('click', (event) => {
      if (event.target === $('permissionModal')) closePermissionModal();
    });
    $('permissionModalSave').addEventListener('click', () => savePermissionModal().catch((err) => {
      $('permissionModalSave').disabled = false;
      toast(err.message);
    }));
    $('saveBtn').addEventListener('click', () => saveUser().catch((err) => toast(err.message)));
    document.addEventListener('keydown', closeTopModalOnEscape);
    $('logoutBtn').addEventListener('click', async () => {
      try { await request('/api/auth/logout', { method: 'POST' }); } catch (err) {}
      window.location.href = '/login';
    });
    clearForm();
    loadCurrentUser().then(loadUsers).catch((err) => toast(err.message));
  </script>
</body>
</html>
"""


OPERATOR_PAGE = r"""
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Protocol Reg 操作员工作台</title>
  <style>
    :root {
      --ink: #11100d;
      --paper: #f5efe2;
      --paper-2: #ebe1cf;
      --line: rgba(17, 16, 13, .16);
      --muted: #6f6659;
      --accent: #d8612c;
      --accent-2: #0f6b5f;
      --good: #2d7a46;
      --bad: #a33b2f;
      --shadow: 0 24px 80px rgba(48, 39, 25, .16);
      --mono: "JetBrains Mono", "Cascadia Code", Consolas, monospace;
      --display: "Fraunces", "Iowan Old Style", Georgia, serif;
      --body: "Aptos", "Gill Sans", "Trebuchet MS", sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--ink);
      font-family: var(--body);
      background:
        radial-gradient(circle at 20% 10%, rgba(216, 97, 44, .22), transparent 30%),
        radial-gradient(circle at 95% 0%, rgba(15, 107, 95, .18), transparent 28%),
        linear-gradient(135deg, #f8f0df 0%, #efe4d2 52%, #e3d3bd 100%);
      overflow-x: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: 0;
      pointer-events: none;
      opacity: .35;
      background-image:
        linear-gradient(rgba(17,16,13,.045) 1px, transparent 1px),
        linear-gradient(90deg, rgba(17,16,13,.045) 1px, transparent 1px);
      background-size: 26px 26px;
      mask-image: linear-gradient(to bottom, black, transparent 85%);
    }
    .shell { width: min(1440px, calc(100vw - 32px)); margin: 0 auto; padding: 28px 0 36px; position: relative; }
    .topbar, .panel {
      border: 1px solid var(--line);
      background: rgba(245, 239, 226, .78);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
      border-radius: 20px;
    }
    .topbar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 18px;
      padding: 12px 20px;
      margin-bottom: 16px;
      position: relative;
      overflow: hidden;
      border-radius: 22px;
      animation: rise .45s ease both;
    }
    .topbar::after {
      content: "";
      position: absolute;
      width: 120px;
      height: 120px;
      right: -50px;
      top: -55px;
      border-radius: 50%;
      border: 22px solid rgba(216, 97, 44, .14);
      pointer-events: none;
    }
    .brand {
      display: flex;
      flex-direction: column;
      gap: 3px;
      flex: 0 0 auto;
      position: relative;
      z-index: 1;
    }
    .brand-eyebrow {
      font-family: var(--mono);
      font-size: 10px;
      letter-spacing: .18em;
      color: var(--accent-2);
      text-transform: uppercase;
    }
    .brand-mark {
      font-family: var(--display);
      font-size: 24px;
      line-height: 1;
      letter-spacing: 0;
    }
    .page-links {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }
    .nav-main {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 12px;
      flex-wrap: wrap;
    }
    .top-actions {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
      flex: 1 1 auto;
      position: relative;
      z-index: 1;
    }
    a, button {
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 999px;
      min-height: 36px;
      padding: 8px 12px;
      background: rgba(255,255,255,.38);
      color: var(--ink);
      text-decoration: none;
      font: 13px/1.2 inherit;
      cursor: pointer;
      white-space: nowrap;
      transition: background .16s ease, border-color .16s ease, color .16s ease, transform .16s ease;
    }
    .page-nav {
      min-height: 42px;
      padding-inline: 16px;
      border-radius: 12px;
    }
    .page-nav.is-active {
      background: var(--ink);
      color: #fff;
      border-color: var(--ink);
      box-shadow: 0 12px 28px rgba(17,16,13,.18);
    }
    button:hover:not(:disabled), a:hover { transform: translateY(-1px); }
    button:disabled {
      opacity: .42;
      cursor: not-allowed;
      transform: none;
    }
    button.primary { color: #fff; background: var(--accent); border-color: var(--accent); }
    button.claim { color: #fff; background: var(--accent-2); border-color: var(--accent-2); }
    button.good { color: #fff; background: var(--good); border-color: var(--good); }
    button.bad { color: #fff; background: var(--bad); border-color: var(--bad); }
    .badge {
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 999px;
      padding: 8px 10px;
      color: var(--muted);
      font: 12px/1 var(--mono);
    }
    .panel {
      overflow: hidden;
    }
    .workspace-head {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      padding: 16px;
      border-bottom: 1px solid rgba(17,16,13,.1);
      align-items: center;
      flex-wrap: wrap;
    }
    .toolbar {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      align-items: center;
    }
    .field-inline {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      min-height: 38px;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 999px;
      padding: 4px 6px 4px 12px;
      background: rgba(255,255,255,.38);
      color: var(--muted);
      font: 12px/1 var(--mono);
    }
    input {
      border: 1px solid rgba(17,16,13,.14);
      background: rgba(255,255,255,.55);
      color: var(--ink);
      border-radius: 999px;
      min-height: 32px;
      padding: 8px 10px;
      font: 14px/1.2 var(--body);
      outline: none;
    }
    input[type="number"] { width: 78px; }
    .queue-summary {
      display: grid;
      grid-template-columns: repeat(5, minmax(92px, 1fr));
      gap: 8px;
      min-width: min(520px, 100%);
    }
    .summary-item {
      border: 1px solid rgba(17,16,13,.1);
      border-radius: 12px;
      padding: 9px 11px;
      background: rgba(255,255,255,.34);
    }
    .summary-label {
      color: var(--muted);
      font: 11px/1.1 var(--mono);
    }
    .summary-value {
      margin-top: 5px;
      font-size: 20px;
      line-height: 1;
      font-weight: 800;
    }
    .table-shell {
      max-height: calc(100vh - 210px);
      overflow: auto;
    }
    table {
      width: 100%;
      min-width: 980px;
      border-collapse: separate;
      border-spacing: 0;
      table-layout: fixed;
    }
    col.email { width: 31%; }
    col.status { width: 15%; }
    col.checkout { width: 15%; }
    col.operator { width: 12%; }
    col.note { width: 14%; }
    col.actions { width: 13%; }
    th, td {
      text-align: left;
      padding: 13px 10px;
      border-bottom: 1px solid rgba(17,16,13,.1);
      vertical-align: middle;
      font-size: 13px;
    }
    th {
      position: sticky;
      top: 0;
      z-index: 1;
      color: var(--muted);
      font: 11px/1.2 var(--mono);
      background: rgba(248,241,229,.96);
      backdrop-filter: blur(14px);
    }
    tbody tr {
      background: rgba(255,255,255,.16);
      transition: background .16s ease;
    }
    tbody tr:hover { background: rgba(255,255,255,.34); }
    tbody tr.is-claimed { box-shadow: inset 3px 0 0 rgba(216,97,44,.72); }
    tbody tr.is-marked { box-shadow: inset 3px 0 0 rgba(45,122,70,.72); }
    tbody tr.is-verified { box-shadow: inset 3px 0 0 rgba(45,122,70,.86); }
    tbody tr.is-failed { box-shadow: inset 3px 0 0 rgba(163,59,47,.72); }
    td code { font-family: var(--mono); font-size: 12px; word-break: break-all; }
    .email-main {
      font-weight: 800;
      word-break: break-all;
    }
    .meta-line {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      align-items: center;
      margin-top: 6px;
      color: var(--muted);
      font-size: 12px;
    }
    .mini-pill, .status-pill, .checkout-link, .checkout-copy {
      display: inline-flex;
      align-items: center;
      width: fit-content;
      border-radius: 999px;
      border: 1px solid rgba(17,16,13,.1);
      background: rgba(255,255,255,.38);
      padding: 5px 8px;
      line-height: 1;
      font-size: 12px;
    }
    .status-pill {
      font-weight: 800;
      margin-bottom: 6px;
    }
    .status-pending { color: var(--accent-2); background: rgba(15,107,95,.09); border-color: rgba(15,107,95,.18); }
    .status-claimed { color: var(--accent); background: rgba(216,97,44,.11); border-color: rgba(216,97,44,.22); }
    .status-marked { color: var(--good); background: rgba(45,122,70,.1); border-color: rgba(45,122,70,.2); }
    .status-verified { color: var(--good); background: rgba(45,122,70,.1); border-color: rgba(45,122,70,.24); }
    .status-failed { color: var(--bad); background: rgba(163,59,47,.1); border-color: rgba(163,59,47,.2); }
    .checkout-link {
      color: var(--accent-2);
      text-decoration: none;
      font-weight: 800;
    }
    .checkout-stack {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .checkout-actions {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      align-items: center;
    }
    .checkout-copy {
      color: #fff;
      background: var(--accent-2);
      border-color: var(--accent-2);
      font-weight: 800;
    }
    .checkout-copy.copied {
      background: var(--good);
      border-color: var(--good);
    }
    .muted {
      color: var(--muted);
      font-size: 12px;
    }
    .operator-chip {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 6px 9px;
      background: rgba(255,255,255,.36);
      border: 1px solid rgba(17,16,13,.1);
      color: var(--ink);
      font-size: 12px;
    }
    .action-row {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      align-items: center;
    }
    .action-row button {
      min-width: 58px;
      padding-inline: 11px;
      font-weight: 800;
    }
    .action-stack {
      display: grid;
      gap: 8px;
    }
    .row-note {
      width: 100%;
      min-height: 34px;
      border-radius: 12px;
      padding: 8px 10px;
      font: 13px/1.2 var(--body);
    }
    .empty {
      padding: 42px 16px;
      color: var(--muted);
      text-align: center;
      border-bottom: 1px solid rgba(17,16,13,.1);
    }
    .toast {
      position: fixed;
      right: 22px;
      bottom: 22px;
      opacity: 0;
      transform: translateY(10px);
      color: #fff;
      background: var(--ink);
      border-radius: 14px;
      padding: 12px 14px;
      transition: .18s ease;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
    @keyframes rise {
      from { opacity: 0; transform: translateY(18px); }
      to { opacity: 1; transform: translateY(0); }
    }
    @media (max-width: 920px) {
      .workspace-head { align-items: stretch; }
      .toolbar { width: 100%; }
      .queue-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); min-width: 0; width: 100%; }
      .table-shell { max-height: none; }
      .topbar { align-items: stretch; }
      .top-actions { justify-content: flex-start; }
      .nav-main { justify-content: flex-start; width: 100%; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header class="topbar">
      <div class="brand">
        <div class="brand-eyebrow">Protocol Reg</div>
        <div class="brand-mark" id="brandMark">账号库控制台</div>
      </div>
      <div class="top-actions">
        <nav class="page-links" id="pageLinks" aria-label="页面导航"></nav>
        <div class="nav-main">
          <span class="badge" id="currentUser">加载中</span>
          <span class="badge" id="queueCount">待加载</span>
          <button id="refreshBtn" type="button">刷新</button>
          <button id="logoutBtn" type="button">退出</button>
        </div>
      </div>
    </header>
    <section class="panel">
      <div class="workspace-head">
        <div class="toolbar">
          <label class="field-inline" title="每页条数">
            <span>每页</span>
            <input id="pageSize" type="number" min="10" max="200" value="30" />
          </label>
          <button class="primary" id="reloadBtn" type="button">重新加载</button>
        </div>
        <div class="queue-summary" id="queueSummary"></div>
      </div>
      <div class="table-shell">
        <table>
          <colgroup>
            <col class="email" />
            <col class="status" />
            <col class="checkout" />
            <col class="operator" />
            <col class="note" />
            <col class="actions" />
          </colgroup>
          <thead>
            <tr>
              <th>邮箱</th>
              <th>状态</th>
              <th>Checkout</th>
              <th>领取人</th>
              <th>备注</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
    </section>
  </main>
  <div class="toast" id="toast"></div>
  <script>
    const $ = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
    let items = [];
    let currentUser = {};
    let queueTimer = null;
    let queueLoadingPromise = null;
    function hasValue(value) {
      const text = String(value ?? '').trim();
      return Boolean(text && text.toLowerCase() !== 'null');
    }
    function toast(message) {
      const node = $('toast');
      node.textContent = message;
      node.classList.add('show');
      clearTimeout(window.__toastTimer);
      window.__toastTimer = setTimeout(() => node.classList.remove('show'), 2200);
    }
    async function request(url, options = {}) {
      const response = await fetch(url, { headers: { 'content-type': 'application/json' }, ...options });
      if (response.status === 401) {
        window.location.href = '/login';
        throw new Error('未登录或会话已过期');
      }
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || '请求失败');
      return data;
    }
    function formatDate(value) {
      if (!value || value === 'null') return '无';
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString('zh-CN', { hour12: false });
    }
    function statusTone(value) {
      return {
        '待订阅': 'pending',
        '处理中': 'claimed',
        '已点击订阅': 'marked',
        '已确认订阅': 'verified',
        '订阅失败': 'failed',
      }[value] || 'pending';
    }
    function statusClass(value) {
      return `status-${statusTone(value)}`;
    }
    function statusLabel(value) {
      return value || '待订阅';
    }
    function renderPageLinks(user) {
      const role = String(user?.role || '').toLowerCase();
      const links = role === 'admin'
        ? [
            ['/', '账号管理'],
            ['/tasks', '任务页面'],
            ['/settings', '设置'],
            ['/operator', '订阅处理'],
            ['/admin/users', '用户管理'],
          ]
        : [
            ['/operator', '订阅处理'],
          ];
      $('pageLinks').innerHTML = links.map(([href, label]) => `
        <a class="page-nav${href === window.location.pathname ? ' is-active' : ''}" href="${escapeHtml(href)}">${escapeHtml(label)}</a>
      `).join('');
    }
    function hasPermission(name) {
      if (currentUser.role === 'admin') return true;
      const permissions = Array.isArray(currentUser.permissions) ? currentUser.permissions : [];
      return permissions.includes('*') || permissions.includes(name);
    }
    function sameOperator(item) {
      return Boolean(currentUser.id) && String(item.subscription_operator_id || '') === String(currentUser.id || '');
    }
    function claimExpired(item) {
      const value = item.subscription_claim_expires_at;
      if (!value || value === 'null') return true;
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return true;
      return date.getTime() <= Date.now();
    }
    function statusMeta(item) {
      const status = item.subscription_status || '待订阅';
      const attempts = Number(item.subscription_verify_attempts || 0);
      const lastMessage = hasValue(item.subscription_verify_last_message) ? item.subscription_verify_last_message : '';
      if (status === '处理中') {
        return hasValue(item.subscription_claim_expires_at)
          ? `到期 ${formatDate(item.subscription_claim_expires_at)}`
          : '处理中';
      }
      if (status === '订阅失败') return '可重新领取';
      if (status === '已点击订阅') {
        const parts = ['自动核实中'];
        if (attempts > 0) parts.push(`第 ${attempts} 次`);
        if (hasValue(item.subscription_verify_next_at)) parts.push(`下次 ${formatDate(item.subscription_verify_next_at)}`);
        if (lastMessage) {
          parts.push(lastMessage.startsWith('当前订阅类型为 ')
            ? `当前仍为 ${lastMessage.replace('当前订阅类型为 ', '')}`
            : lastMessage);
        }
        return parts.join(' · ');
      }
      if (status === '已确认订阅') {
        return lastMessage || '已确认';
      }
      return '等待领取';
    }
    async function clipboardWrite(text) {
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(text);
          return;
        } catch (err) {
          // fallback below
        }
      }
      const textarea = document.createElement('textarea');
      textarea.value = text;
      textarea.setAttribute('readonly', '');
      textarea.style.position = 'fixed';
      textarea.style.top = '0';
      textarea.style.left = '0';
      textarea.style.opacity = '0';
      textarea.style.pointerEvents = 'none';
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      textarea.setSelectionRange(0, text.length);
      let ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (err) {
        ok = false;
      }
      document.body.removeChild(textarea);
      if (!ok) throw new Error('当前环境不支持复制');
    }
    async function copyCheckoutLink(value, node) {
      const text = String(value || '').trim();
      if (!hasValue(text)) return toast('没有可复制内容');
      await clipboardWrite(text);
      toast('Checkout 链接已复制');
      if (node) {
        node.classList.add('copied');
        setTimeout(() => node.classList.remove('copied'), 700);
      }
    }
    async function loadCurrentUser() {
      const data = await request('/api/auth/me');
      const user = data.user || {};
      currentUser = user;
      renderPageLinks(user);
      $('currentUser').textContent = `${user.username || '未知'} · ${user.role === 'admin' ? '管理员' : '操作员'}`;
    }
    async function loadQueue() {
      if (queueLoadingPromise) return queueLoadingPromise;
      queueLoadingPromise = (async () => {
        const pageSize = Number($('pageSize').value || 30);
        const data = await request(`/api/operator/subscriptions?page=1&page_size=${encodeURIComponent(pageSize)}`);
        items = data.items || [];
        const verifyingCount = items.filter((item) => (item.subscription_status || '待订阅') === '已点击订阅').length;
        $('queueCount').textContent = verifyingCount > 0 ? `数量 ${items.length} · 自动核实 ${verifyingCount}` : `数量 ${items.length}`;
        renderQueueSummary();
        renderRows();
      })();
      try {
        return await queueLoadingPromise;
      } finally {
        queueLoadingPromise = null;
      }
    }
    function renderQueueSummary() {
      const counts = { pending: 0, claimed: 0, marked: 0, verified: 0, failed: 0 };
      for (const item of items) {
        const tone = statusTone(item.subscription_status || '待订阅');
        if (tone in counts) counts[tone] += 1;
      }
      const cards = [
        ['待订阅', counts.pending],
        ['处理中', counts.claimed],
        ['待核实', counts.marked],
        ['已确认', counts.verified],
        ['失败', counts.failed],
      ];
      $('queueSummary').innerHTML = cards.map(([label, value]) => `
        <div class="summary-item">
          <div class="summary-label">${escapeHtml(label)}</div>
          <div class="summary-value">${escapeHtml(value)}</div>
        </div>
      `).join('');
    }
    function renderRows() {
      if (!items.length) {
        $('rows').innerHTML = '<tr><td class="empty" colspan="6">暂无待订阅账号</td></tr>';
        return;
      }
      $('rows').innerHTML = items.map((item) => {
        const status = item.subscription_status || '待订阅';
        const tone = statusTone(status);
        const owned = sameOperator(item);
        const claimedByRaw = item.subscription_operator_id;
        const operatorName = item.subscription_operator_name || (hasValue(claimedByRaw) ? `#${claimedByRaw}` : '未领取');
        const claimedBy = hasValue(claimedByRaw)
          ? (owned ? '我' : operatorName)
          : '未领取';
        const note = hasValue(item.subscription_note)
          ? `<code>${escapeHtml(item.subscription_note)}</code>`
          : '<span class="muted">无</span>';
        const checkout = hasValue(item.checkout_url)
          ? `
            <div class="checkout-stack">
              <div class="checkout-actions">
                <a class="checkout-link" href="${escapeHtml(item.checkout_url)}" target="_blank" rel="noopener noreferrer">打开链接</a>
                <button type="button" class="checkout-copy" data-copy-checkout="${escapeHtml(item.checkout_url)}">复制链接</button>
              </div>
            </div>
          `
          : '<span class="muted">领取后可复制</span>';
        const canClaim = hasPermission('claim_subscription_account') && (status === '待订阅' || status === '订阅失败' || (status === '处理中' && (owned || claimExpired(item))));
        const canFinish = status === '处理中' && owned && hasPermission('mark_subscription_done');
        const canFail = status === '处理中' && owned && hasPermission('mark_subscription_failed');
        const canRelease = status === '处理中' && owned && hasPermission('claim_subscription_account');
        const actions = (() => {
          if (status === '处理中') {
            if (owned) {
              return `
                <div class="action-stack">
                  <input class="row-note" data-note-for="${escapeHtml(item.id)}" value="${hasValue(item.subscription_note) ? escapeHtml(item.subscription_note) : ''}" placeholder="备注（可选）" />
                  <div class="action-row">
                    <button type="button" data-submitted="${escapeHtml(item.id)}" class="good" ${canFinish ? '' : 'disabled'}>已订阅</button>
                    <button type="button" data-failed="${escapeHtml(item.id)}" class="bad" ${canFail ? '' : 'disabled'}>失败</button>
                    <button type="button" data-release="${escapeHtml(item.id)}" ${canRelease ? '' : 'disabled'}>释放</button>
                  </div>
                </div>
              `;
            }
            if (canClaim) {
              return `<button type="button" data-claim="${escapeHtml(item.id)}" class="claim">${status === '订阅失败' ? '重新领取' : '领取'}</button>`;
            }
            return '<span class="muted">处理中</span>';
          }
          if (status === '已点击订阅') return '<span class="muted">自动核实中</span>';
          if (status === '已确认订阅') return '<span class="muted">已确认</span>';
          if (canClaim) {
            return `<button type="button" data-claim="${escapeHtml(item.id)}" class="claim">${status === '订阅失败' ? '重新领取' : (owned ? '续领' : '领取')}</button>`;
          }
          return '<span class="muted">待处理</span>';
        })();
        return `
          <tr class="is-${tone}">
            <td>
              <div class="email-main">${escapeHtml(item.email)}</div>
              <div class="meta-line"><span class="mini-pill">${escapeHtml(hasValue(item.subscription_type) ? item.subscription_type : '无')}</span></div>
            </td>
            <td>
              <div class="status-pill ${statusClass(status)}">${escapeHtml(statusLabel(status))}</div>
              <div class="muted">${escapeHtml(statusMeta(item))}</div>
            </td>
            <td>${checkout}</td>
            <td><span class="operator-chip">${escapeHtml(claimedBy)}</span></td>
            <td>${note}</td>
            <td>${actions}</td>
          </tr>
        `;
      }).join('');
      document.querySelectorAll('[data-claim]').forEach((button) => {
        button.addEventListener('click', () => claimAccount(button.dataset.claim).catch((err) => toast(err.message)));
      });
      document.querySelectorAll('[data-submitted]').forEach((button) => {
        button.addEventListener('click', () => markSubmitted(button.dataset.submitted).catch((err) => toast(err.message)));
      });
      document.querySelectorAll('[data-failed]').forEach((button) => {
        button.addEventListener('click', () => markFailed(button.dataset.failed).catch((err) => toast(err.message)));
      });
      document.querySelectorAll('[data-release]').forEach((button) => {
        button.addEventListener('click', () => releaseAccount(button.dataset.release).catch((err) => toast(err.message)));
      });
      document.querySelectorAll('[data-copy-checkout]').forEach((button) => {
        button.addEventListener('click', () => copyCheckoutLink(button.dataset.copyCheckout, button).catch((err) => toast(err.message)));
      });
    }
    async function claimAccount(id) {
      await request(`/api/operator/subscriptions/${encodeURIComponent(id)}/claim`, {
        method: 'POST',
        body: JSON.stringify({ claim_minutes: 30 }),
      });
      toast('任务已领取');
      await loadQueue();
    }
    function noteForRow(id) {
      const input = document.querySelector(`[data-note-for="${String(id)}"]`);
      return input ? input.value.trim() : '';
    }
    async function markSubmitted(id) {
      const note = noteForRow(id);
      const data = await request(`/api/operator/subscriptions/${encodeURIComponent(id)}/mark-subscribed`, {
        method: 'POST',
        body: JSON.stringify({ note: note || '' }),
      });
      if (data.auto_verified) {
        toast(`系统已核实为 ${data.plan_type || '已确认订阅'}`);
      } else if (data.verification_pending) {
        const reason = data.verification_message || '';
        if (reason.startsWith('当前订阅类型为 ')) {
          toast(`已标记，自动核实中：当前仍为 ${reason.replace('当前订阅类型为 ', '')}`);
        } else {
          toast(reason ? `已标记，自动核实中：${reason}` : '已标记，自动核实中');
        }
      } else {
        toast('已标记订阅');
      }
      await loadQueue();
    }
    async function markFailed(id) {
      const note = noteForRow(id);
      await request(`/api/operator/subscriptions/${encodeURIComponent(id)}/mark-failed`, {
        method: 'POST',
        body: JSON.stringify({ note: note || '' }),
      });
      toast('已标记失败');
      await loadQueue();
    }
    async function releaseAccount(id) {
      await request(`/api/operator/subscriptions/${encodeURIComponent(id)}/release`, {
        method: 'POST',
      });
      toast('已释放任务');
      await loadQueue();
    }
    $('reloadBtn').addEventListener('click', () => loadQueue().catch((err) => toast(err.message)));
    $('refreshBtn').addEventListener('click', () => loadQueue().catch((err) => toast(err.message)));
    $('logoutBtn').addEventListener('click', async () => {
      try { await request('/api/auth/logout', { method: 'POST' }); } catch (err) {}
      window.location.href = '/login';
    });
    function startQueuePolling() {
      if (queueTimer) return;
      queueTimer = setInterval(() => {
        if (!document.hidden) {
          loadQueue().catch(() => {});
        }
      }, 3000);
    }
    loadCurrentUser()
      .then(loadQueue)
      .then(startQueuePolling)
      .catch((err) => toast(err.message));
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
