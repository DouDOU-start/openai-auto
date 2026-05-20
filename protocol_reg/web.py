from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import secrets
import sqlite3
import socket
import string
import threading
import time
import webbrowser
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .config import AppConfig, load_app_config
from .flow import RegisterFlow
from .settings import Settings
from .storage import (
    NULL_VALUE,
    account_stats_db,
    delete_account_db,
    export_accounts_db_to_txt,
    get_account_db,
    list_account_rows,
    save_account,
    save_account_storage,
    save_account_db_record,
    save_login_session,
    sync_account_storage,
    try_load_login_session,
)
from .utils import make_password


class AccountPayload(BaseModel):
    email: str
    password: str
    subscription_type: str = NULL_VALUE
    refresh_token: str = NULL_VALUE
    session_json: str = NULL_VALUE
    status: str = "active"
    source: str = "manual"


class OperationPayload(BaseModel):
    mode: str
    email: str = ""
    password: str = ""
    account_id: int | None = None
    generate_email: bool = False
    generate_password: bool = False
    create_checkout: bool = False


class PromptPayload(BaseModel):
    value: str


@dataclass(frozen=True)
class WebRuntime:
    repo_root: Path
    accounts_path: Path
    sessions_path: Path
    token_path: Path
    checkout_path: Path
    config_path: Path
    license_file: Path | None
    proxy: str = ""
    login_delay: int = 20
    timeout: int = 30
    ssl_verify: bool = True


@dataclass
class WebJob:
    id: str
    mode: str
    email: str
    status: str = "pending"
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

    def wait_input(self, prompt: str) -> str:
        with self._condition:
            self.prompt = prompt
            self.status = "waiting"
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


class JobManager:
    def __init__(self, runtime: WebRuntime):
        self.runtime = runtime
        self._jobs: dict[str, WebJob] = {}
        self._lock = threading.RLock()

    def start(self, payload: OperationPayload) -> WebJob:
        mode = payload.mode.strip().lower()
        if mode not in {"register", "login", "authorize"}:
            raise ValueError("运行模式必须是 register、login 或 authorize")
        job = WebJob(id=secrets.token_hex(8), mode=mode, email=payload.email.strip().lower())
        with self._lock:
            active = [item for item in self._jobs.values() if item.status in {"pending", "running", "waiting"}]
            if active:
                raise ValueError("已有任务正在执行，请等待完成后再启动新任务")
            self._jobs[job.id] = job
        thread = threading.Thread(target=self._run, args=(job, payload), daemon=True)
        thread.start()
        return job

    def get(self, job_id: str) -> WebJob | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self) -> list[WebJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)[:20]

    def _run(self, job: WebJob, payload: OperationPayload) -> None:
        writer = JobWriter(job)
        with redirect_stdout(writer), redirect_stderr(writer):
            flow: RegisterFlow | None = None
            try:
                job.status = "running"
                job.updated_at = time.time()
                settings = _settings_from_runtime(self.runtime)
                cfg = load_app_config(self.runtime.config_path)
                email, password = _resolve_operation_account(payload, cfg, self.runtime)
                job.email = email
                job.log(f"[任务] 开始执行 {payload.mode}: {email}")
                flow = RegisterFlow(settings, prompt=job.wait_input)
                result = _execute_operation(job, payload, settings, flow, email, password, self.runtime)
                job.result = result
                job.status = "succeeded"
                job.log("[任务] 执行完成")
            except Exception as exc:
                job.error = str(exc)
                job.status = "failed"
                job.log(f"[错误] {exc}")
            finally:
                if flow is not None:
                    flow.close()
                writer.flush()
                job.updated_at = time.time()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def create_app(
    accounts_output: Path | None = None,
    session_file: Path | None = None,
    runtime: WebRuntime | None = None,
) -> FastAPI:
    repo_root = _repo_root()
    accounts_path = (accounts_output or repo_root / "data" / "accounts.txt").resolve()
    sessions_path = (session_file or repo_root / "data" / "sessions.json").resolve()
    if runtime is None:
        runtime = WebRuntime(
            repo_root=repo_root,
            accounts_path=accounts_path,
            sessions_path=sessions_path,
            token_path=(repo_root / "data" / "tokens.jsonl").resolve(),
            checkout_path=(repo_root / "data" / "checkout_urls.jsonl").resolve(),
            config_path=(repo_root / "config" / "protocol-reg.yaml").resolve(),
            license_file=_default_license_file(repo_root),
        )
    accounts_path = runtime.accounts_path
    sessions_path = runtime.sessions_path
    manager = JobManager(runtime)

    app = FastAPI(title="Protocol Reg 账号管理", version="0.1.0")

    @app.on_event("startup")
    def startup() -> None:
        sync_account_storage(accounts_path, sessions_path)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return HTML_PAGE

    @app.get("/api/accounts")
    def api_accounts(
        search: str = "",
        status: str = Query("all"),
        plan: str = Query("all"),
    ) -> dict[str, Any]:
        rows = list_account_rows(search=search, status=status, plan=plan)
        return {
            "items": [_account_summary(row) for row in rows],
            "stats": account_stats_db(),
        }

    @app.get("/api/accounts/{account_id}")
    def api_account_detail(account_id: int) -> dict[str, Any]:
        account = get_account_db(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        return {"item": _account_detail(account)}

    @app.post("/api/accounts")
    def api_create_account(payload: AccountPayload) -> dict[str, Any]:
        data = _payload_data(payload)
        try:
            saved = save_account_db_record(data)
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=400, detail="邮箱已存在，不能保存重复账号") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        export_accounts_db_to_txt(accounts_path)
        return {"item": _account_detail(saved), "stats": account_stats_db()}

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
        export_accounts_db_to_txt(accounts_path)
        return {"item": _account_detail(saved), "stats": account_stats_db()}

    @app.delete("/api/accounts/{account_id}")
    def api_delete_account(account_id: int) -> dict[str, Any]:
        deleted = delete_account_db(account_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="账号不存在")
        export_accounts_db_to_txt(accounts_path)
        return {"deleted": True, "stats": account_stats_db()}

    @app.post("/api/sync")
    def api_sync() -> dict[str, Any]:
        sync_account_storage(accounts_path, sessions_path)
        return {"ok": True, "stats": account_stats_db()}

    @app.post("/api/export")
    def api_export() -> dict[str, Any]:
        export_accounts_db_to_txt(accounts_path)
        return {"ok": True, "path": str(accounts_path), "stats": account_stats_db()}

    @app.post("/api/ops/jobs")
    def api_start_job(payload: OperationPayload) -> dict[str, Any]:
        try:
            job = manager.start(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"job": _job_view(job)}

    @app.get("/api/ops/jobs")
    def api_jobs() -> dict[str, Any]:
        return {"items": [_job_view(job) for job in manager.list_recent()]}

    @app.get("/api/ops/jobs/{job_id}")
    def api_job(job_id: str) -> dict[str, Any]:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return {"job": _job_view(job)}

    @app.post("/api/ops/jobs/{job_id}/input")
    def api_job_input(job_id: str, payload: PromptPayload) -> dict[str, Any]:
        job = manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        if job.status != "waiting":
            raise HTTPException(status_code=400, detail="任务当前不在等待输入状态")
        job.submit_input(payload.value)
        return {"job": _job_view(job)}

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


def _account_summary(account: dict[str, str]) -> dict[str, Any]:
    return {
        "id": int(account.get("id") or 0),
        "email": account.get("email", ""),
        "subscription_type": account.get("subscription_type", NULL_VALUE),
        "status": account.get("status", NULL_VALUE),
        "source": account.get("source", NULL_VALUE),
        "updated_at": account.get("updated_at", NULL_VALUE),
        "last_login_at": account.get("last_login_at", NULL_VALUE),
        "last_authorized_at": account.get("last_authorized_at", NULL_VALUE),
        "has_password": _has_value(account.get("password")),
        "has_refresh_token": _has_value(account.get("refresh_token")),
        "has_session": _has_value(account.get("session")),
        "password_preview": _secret_preview(account.get("password")),
        "refresh_token_preview": _secret_preview(account.get("refresh_token")),
        "session_preview": _secret_preview(account.get("session")),
    }


def _account_detail(account: dict[str, str]) -> dict[str, Any]:
    detail = _account_summary(account)
    detail.update(
        {
            "password": account.get("password", NULL_VALUE),
            "refresh_token": account.get("refresh_token", NULL_VALUE),
            "session_json": account.get("session", NULL_VALUE),
            "created_at": account.get("created_at", NULL_VALUE),
        }
    )
    return detail


def _job_view(job: WebJob) -> dict[str, Any]:
    return {
        "id": job.id,
        "mode": job.mode,
        "email": job.email,
        "status": job.status,
        "prompt": job.prompt,
        "error": job.error,
        "result": job.result,
        "logs": job.logs[-300:],
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


def _settings_from_runtime(runtime: WebRuntime) -> Settings:
    cfg = load_app_config(runtime.config_path)
    proxy = runtime.proxy.strip() or os.environ.get("PROTOCOL_REG_PROXY", "").strip() or cfg.proxy
    email_code_api = os.environ.get("EMAIL_CODE_API", "").strip() or cfg.email_code_api
    email_code_key = os.environ.get("EMAIL_CODE_API_KEY", "").strip() or cfg.email_code_key
    email_code_sender_suffix = (
        os.environ.get("EMAIL_CODE_SENDER_SUFFIX", "").strip()
        or cfg.email_code_sender_suffix
        or "openai.com"
    )
    email_code_timeout = int(os.environ.get("EMAIL_CODE_TIMEOUT", "0") or 0) or cfg.email_code_timeout
    email_code_poll = float(os.environ.get("EMAIL_CODE_POLL", "0") or 0) or cfg.email_code_poll
    return Settings(
        project_root=runtime.repo_root.resolve(),
        proxy=proxy,
        output=runtime.accounts_path,
        session_file=runtime.sessions_path,
        license_file=runtime.license_file,
        login_delay=max(0, runtime.login_delay),
        timeout=max(1, runtime.timeout),
        ssl_verify=runtime.ssl_verify,
        email_code_api_base=str(email_code_api or "").strip(),
        email_code_api_key=str(email_code_key or "").strip(),
        email_code_sender_suffix=str(email_code_sender_suffix or "openai.com").strip() or "openai.com",
        email_code_poll_interval=max(0.5, float(email_code_poll)),
        email_code_timeout=max(5, int(email_code_timeout)),
    )


def _resolve_operation_account(payload: OperationPayload, cfg: AppConfig, runtime: WebRuntime) -> tuple[str, str]:
    account = get_account_db(payload.account_id) if payload.account_id else None
    if payload.account_id and account is None:
        raise ValueError(f"账号不存在: {payload.account_id}")
    email = str(payload.email or "").strip().lower()
    password = str(payload.password or "")
    if account is not None:
        email = email or account.get("email", "")
        password = password or account.get("password", "")

    if payload.mode == "register" and not email and payload.generate_email:
        email = _random_email(cfg.email_suffixes)
    if payload.mode == "register" and not password and payload.generate_password:
        password = make_password()

    if not email:
        raise ValueError("邮箱不能为空")
    if payload.mode == "register" and _email_exists(email):
        raise ValueError(f"邮箱已存在于数据库，避免重复注册: {email}")
    if payload.mode in {"register", "login"} and not password:
        raise ValueError("密码不能为空")
    if payload.mode == "authorize" and not password:
        session_data = try_load_login_session(runtime.sessions_path, email)
        if session_data is None:
            raise ValueError("授权需要已有登录会话或账号密码")
    return email, password


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
        save_account_storage(settings.output, email, password, token_data, source="register")
        checkout = None
        if payload.create_checkout:
            checkout = flow.create_plus_trial_checkout(
                flow.http.session,
                token_data.get("chatgpt_session") if isinstance(token_data.get("chatgpt_session"), dict) else {},
            )
            _save_checkout_if_available(checkout, runtime.checkout_path)
        return {"email": token_data.get("email") or email, "checkout_url": _checkout_long_url(checkout)}

    if mode == "login":
        session_data = flow.login(email, password, create_checkout=payload.create_checkout)
        save_login_session(settings.session_file, email, password, session_data)
        save_account_storage(settings.output, email, password, session_data, source="login")
        checkout = session_data.get("plus_trial_checkout") if payload.create_checkout else None
        _save_checkout_if_available(checkout, runtime.checkout_path)
        return {"email": email, "checkout_url": _checkout_long_url(checkout)}

    session_data = try_load_login_session(settings.session_file, email)
    if session_data is None:
        job.log("[授权] 未找到可用登录会话，使用账号密码即时登录")
        session_data = flow.login(email, password, create_checkout=False)
        save_login_session(settings.session_file, email, password, session_data)
        save_account_storage(settings.output, email, password, session_data, source="login")
    else:
        password = str(session_data.get("password") or password)
    token_data = flow.authorize_from_session(email, session_data)
    save_account(runtime.token_path, email, password, token_data)
    save_account_storage(settings.output, email, password, token_data, source="authorize")
    return {"email": token_data.get("email") or email, "token_output": str(runtime.token_path)}


def _email_exists(email: str) -> bool:
    expected = email.strip().lower()
    return any(account.get("email") == expected for account in list_account_rows())


def _random_email(suffixes: tuple[str, ...]) -> str:
    if not suffixes:
        raise ValueError("随机邮箱需要先在配置文件设置 email_suffixes")
    alphabet = string.ascii_lowercase + string.digits
    existing = {account.get("email", "") for account in list_account_rows()}
    for _ in range(1000):
        suffix = secrets.choice(suffixes)
        local = "oa" + "".join(secrets.choice(alphabet) for _ in range(12))
        email = f"{local}@{suffix}".lower()
        if email not in existing:
            return email
    raise ValueError("随机邮箱生成失败：配置后缀下的候选邮箱均与已有记录冲突")


def _checkout_long_url(checkout: object) -> str:
    if not isinstance(checkout, dict):
        return ""
    for key in ("long_url", "hosted_url", "openai_payurl"):
        url = str(checkout.get(key) or "").strip()
        if url:
            return url
    return ""


def _save_checkout_if_available(checkout: object, output: Path) -> None:
    long_url = _checkout_long_url(checkout)
    if not long_url:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "long_url": long_url,
        "status_code": checkout.get("status_code") if isinstance(checkout, dict) else None,
    }
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


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
    parser.add_argument("--proxy", default="", help="注册/登录/授权代理")
    parser.add_argument("--output", default=str(repo_root / "data" / "accounts.txt"), help="兼容导出的 accounts.txt 路径")
    parser.add_argument("--session-file", default=str(repo_root / "data" / "sessions.json"), help="登录会话 JSON 路径")
    parser.add_argument("--token-output", default=str(repo_root / "data" / "tokens.jsonl"), help="授权 token JSONL 输出路径")
    parser.add_argument("--checkout-output", default=str(repo_root / "data" / "checkout_urls.jsonl"), help="支付链接 JSONL 输出路径")
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

    accounts_output = Path(args.output).resolve()
    session_file = Path(args.session_file).resolve()
    repo_root = _repo_root()
    license_file = Path(args.license_file).resolve() if args.license_file else _default_license_file(repo_root)
    runtime = WebRuntime(
        repo_root=repo_root,
        accounts_path=accounts_output,
        sessions_path=session_file,
        token_path=Path(args.token_output).resolve(),
        checkout_path=Path(args.checkout_output).resolve(),
        config_path=Path(args.config).resolve(),
        license_file=license_file,
        proxy=str(args.proxy or "").strip(),
        login_delay=max(0, args.login_delay),
        timeout=max(1, args.timeout),
        ssl_verify=not args.no_ssl_verify,
    )
    app = create_app(accounts_output=accounts_output, session_file=session_file, runtime=runtime)
    urls = _display_urls(args.host, args.port)
    print("[Web] 账号管理页面:")
    for url in urls:
        print(f"  {url}")
    print(f"[Web] 数据库: {db_path}")
    print(f"[Web] 配置文件: {runtime.config_path}")
    print(f"[Web] accounts.txt 导出: {accounts_output}")
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

    .hero {
      display: grid;
      grid-template-columns: 1.1fr .9fr;
      gap: 24px;
      align-items: stretch;
      margin-bottom: 22px;
      animation: rise .55s ease both;
    }

    .title-card, .stat-card, .panel, .editor {
      border: 1px solid var(--line);
      background: rgba(245, 239, 226, .76);
      box-shadow: var(--shadow);
      backdrop-filter: blur(18px);
    }

    .title-card {
      border-radius: 32px;
      padding: 32px;
      position: relative;
      overflow: hidden;
    }

    .title-card::after {
      content: "";
      position: absolute;
      width: 240px;
      height: 240px;
      right: -70px;
      top: -70px;
      border-radius: 50%;
      border: 42px solid rgba(216, 97, 44, .16);
    }

    .eyebrow {
      font-family: var(--mono);
      letter-spacing: .16em;
      font-size: 12px;
      color: var(--accent-2);
      text-transform: uppercase;
      margin-bottom: 16px;
    }

    h1 {
      margin: 0;
      max-width: 780px;
      font-family: var(--display);
      font-size: clamp(42px, 6vw, 82px);
      line-height: .88;
      letter-spacing: -.06em;
    }

    .hero-text {
      margin: 20px 0 0;
      max-width: 720px;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.8;
    }

    .stats {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }

    .stat-card {
      border-radius: 26px;
      padding: 22px;
      min-height: 128px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      animation: rise .55s ease both;
    }

    .stat-card:nth-child(2) { animation-delay: .05s; }
    .stat-card:nth-child(3) { animation-delay: .1s; }
    .stat-card:nth-child(4) { animation-delay: .15s; }

    .stat-label {
      color: var(--muted);
      font-family: var(--mono);
      font-size: 12px;
    }

    .stat-value {
      font-family: var(--display);
      font-size: 44px;
      line-height: 1;
      letter-spacing: -.05em;
    }

    .workspace {
      display: grid;
      grid-template-columns: minmax(0, 1fr) 470px;
      gap: 22px;
      align-items: start;
    }

    .panel, .editor {
      border-radius: 30px;
      overflow: hidden;
    }

    .toolbar {
      padding: 18px;
      display: grid;
      grid-template-columns: minmax(220px, 1fr) 150px 150px auto auto;
      gap: 10px;
      border-bottom: 1px solid var(--line);
      background: rgba(255, 250, 241, .42);
    }

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

    input:focus, select:focus, textarea:focus {
      border-color: rgba(216, 97, 44, .72);
      box-shadow: 0 0 0 4px rgba(216, 97, 44, .12);
    }

    button {
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
    }

    button:hover { transform: translateY(-2px); filter: brightness(1.05); }
    button:active { transform: translateY(0); }
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
      gap: 12px;
      max-height: calc(100vh - 278px);
      overflow: auto;
    }

    .account-card {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 14px;
      padding: 18px;
      border: 1px solid rgba(17,16,13,.11);
      border-radius: 24px;
      background: rgba(255, 252, 246, .58);
      cursor: pointer;
      transition: transform .18s ease, border-color .18s ease, background .18s ease;
      animation: cardIn .28s ease both;
    }

    .account-card:hover, .account-card.active {
      transform: translateY(-2px);
      border-color: rgba(216, 97, 44, .5);
      background: rgba(255, 250, 241, .9);
    }

    .email {
      font-family: var(--mono);
      font-size: 15px;
      word-break: break-all;
    }

    .meta {
      margin-top: 10px;
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      color: var(--muted);
      font-size: 12px;
    }

    .pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border: 1px solid rgba(17,16,13,.12);
      border-radius: 999px;
      padding: 6px 9px;
      background: rgba(255,255,255,.42);
      font-family: var(--mono);
    }

    .pill.good { color: var(--good); }
    .pill.warn { color: var(--warn); }
    .pill.bad { color: var(--bad); }

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
      letter-spacing: -.04em;
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
    }

    .ops-row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      align-items: stretch;
    }

    .ops-row select { height: 100%; }

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
      .hero { flex: 0 0 auto; }
      .workspace {
        flex: 1;
        min-height: 0;
        align-items: stretch;
      }
      .panel, .editor {
        height: 100%;
        min-height: 0;
        display: flex;
        flex-direction: column;
      }
      .toolbar, .editor-head, .tabs { flex-shrink: 0; }
      .list {
        flex: 1;
        max-height: none;
        min-height: 0;
      }
      .tab-panel { overflow-y: auto; }
      .tab-panel:not(.is-hidden) {
        flex: 1;
        min-height: 0;
      }
    }

    @media (max-width: 1120px) {
      .hero, .workspace { grid-template-columns: 1fr; }
      .list { max-height: none; }
    }

    @media (max-width: 760px) {
      .shell { width: min(100vw - 20px, 1440px); padding-top: 10px; }
      .title-card { padding: 24px; border-radius: 24px; }
      .stats { grid-template-columns: 1fr 1fr; }
      .toolbar { grid-template-columns: 1fr; }
      .grid-2 { grid-template-columns: 1fr; }
      .account-card { grid-template-columns: 1fr; }
      .updated { text-align: left; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="title-card">
        <div class="eyebrow">Protocol Reg / Local Account Console</div>
        <h1>账号库<br />控制台</h1>
        <p class="hero-text">直接管理 SQLite 中的账号记录。列表默认只显示脱敏状态，选择账号后可以编辑密码、订阅类型、RT 和 ChatGPT session，并同步导出兼容的 accounts.txt。</p>
      </div>
      <div class="stats" id="stats"></div>
    </section>

    <section class="workspace">
      <div class="panel">
        <div class="toolbar">
          <input id="search" placeholder="搜索邮箱 / 类型 / 来源" autocomplete="off" />
          <select id="planFilter"><option value="all">全部类型</option></select>
          <select id="statusFilter"><option value="all">全部状态</option></select>
          <button class="secondary" id="syncBtn">同步导入</button>
          <button class="ghost" id="exportBtn">导出 TXT</button>
        </div>
        <div class="list" id="accountList"></div>
      </div>

      <aside class="editor">
        <div class="editor-head">
          <div>
            <div class="editor-title" id="editorTitle">新建账号</div>
            <div class="eyebrow" id="editorHint">保存后写入数据库</div>
          </div>
          <button class="accent" id="newBtn">新建</button>
        </div>
        <nav class="tabs" role="tablist">
          <button class="tab is-active" data-tab="form" role="tab" type="button">账号详情</button>
          <button class="tab" data-tab="ops" role="tab" type="button">
            执行任务
            <span class="tab-badge" id="jobState" data-status="idle">空闲</span>
          </button>
        </nav>
        <section class="tab-panel" data-panel="form">
          <form class="form" id="accountForm">
            <input type="hidden" id="accountId" />
            <div class="field">
              <label>邮箱 <span>唯一键</span></label>
              <input id="email" required placeholder="name@example.com" />
            </div>
            <div class="field">
              <label>密码 <button class="ghost" type="button" data-copy="password">复制</button></label>
              <input id="password" required placeholder="账号密码" />
            </div>
            <div class="grid-2">
              <div class="field">
                <label>订阅类型</label>
                <input id="subscriptionType" placeholder="free / plus / team / null" />
              </div>
              <div class="field">
                <label>状态</label>
                <input id="status" placeholder="active" />
              </div>
            </div>
            <div class="grid-2">
              <div class="field">
                <label>来源</label>
                <input id="source" placeholder="register / login / authorize / manual" />
              </div>
              <div class="field">
                <label>Refresh Token <button class="ghost" type="button" data-copy="refreshToken">复制</button></label>
                <input id="refreshToken" placeholder="没有则留空" />
              </div>
            </div>
            <div class="field">
              <label>Session JSON <button class="ghost" type="button" id="formatSessionBtn">格式化</button></label>
              <textarea id="sessionJson" spellcheck="false" placeholder="/api/auth/session 返回 data 对象，留空保存为 null"></textarea>
            </div>
            <div class="button-row">
              <button type="submit">保存账号</button>
              <button class="ghost" type="button" id="copyLineBtn">复制账号行</button>
              <button class="danger" type="button" id="deleteBtn">删除</button>
            </div>
          </form>
        </section>
        <section class="tab-panel is-hidden" data-panel="ops">
          <div class="ops">
            <div class="ops-row">
              <select id="opMode">
                <option value="register">register 注册</option>
                <option value="login">login 登录</option>
                <option value="authorize">authorize 授权</option>
              </select>
              <button class="secondary" type="button" id="startJobBtn">开始执行</button>
            </div>
            <div class="grid-2">
              <input id="opEmail" placeholder="执行邮箱，留空可使用当前表单" />
              <input id="opPassword" placeholder="执行密码，留空可使用当前表单" />
            </div>
            <div class="checks">
              <label><input type="checkbox" id="opGenerateEmail" /> 注册时随机邮箱</label>
              <label><input type="checkbox" id="opGeneratePassword" /> 注册时随机密码</label>
              <label><input type="checkbox" id="opCreateCheckout" /> 生成 checkout</label>
            </div>
            <div class="prompt-box" id="promptBox">
              <input id="promptInput" placeholder="输入邮箱验证码后继续" />
              <button type="button" id="submitPromptBtn">提交验证码</button>
            </div>
            <div class="job-console" id="jobLog">任务日志会显示在这里。</div>
          </div>
        </section>
      </aside>
    </section>
  </main>
  <div class="toast" id="toast"></div>

  <script>
    const state = { selectedId: null, items: [], stats: {}, currentJobId: null, jobTimer: null };
    const $ = (id) => document.getElementById(id);
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (ch) => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[ch]));
    const hasValue = (value) => Boolean(String(value ?? '').trim() && String(value ?? '').trim().toLowerCase() !== 'null');

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
      const plans = stats.plans || {};
      const topPlan = Object.entries(plans).sort((a, b) => b[1] - a[1])[0];
      const cards = [
        ['账号总数', stats.total ?? 0],
        ['有 RT', stats.with_rt ?? 0],
        ['有 Session', stats.with_session ?? 0],
        ['主类型', topPlan ? `${topPlan[0]} · ${topPlan[1]}` : 'null'],
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
      const planOptions = ['all', ...Object.keys(stats.plans || {}).filter(hasValue)];
      const statusOptions = ['all', ...Object.keys(stats.statuses || {}).filter(hasValue)];
      $('planFilter').innerHTML = planOptions.map((value) => `<option value="${escapeHtml(value)}">${value === 'all' ? '全部类型' : escapeHtml(value)}</option>`).join('');
      $('statusFilter').innerHTML = statusOptions.map((value) => `<option value="${escapeHtml(value)}">${value === 'all' ? '全部状态' : escapeHtml(value)}</option>`).join('');
      $('planFilter').value = planOptions.includes(currentPlan) ? currentPlan : 'all';
      $('statusFilter').value = statusOptions.includes(currentStatus) ? currentStatus : 'all';
    }

    function renderList(items) {
      const list = $('accountList');
      if (!items.length) {
        list.innerHTML = '<div class="empty">没有匹配账号。可以点击右侧新建，或点击“同步导入”从 accounts.txt / sessions.json 导入。</div>';
        return;
      }
      list.innerHTML = items.map((item, index) => {
        const active = Number(item.id) === Number(state.selectedId) ? ' active' : '';
        const rtClass = item.has_refresh_token ? 'good' : 'warn';
        const sessionClass = item.has_session ? 'good' : 'warn';
        return `
          <article class="account-card${active}" data-id="${item.id}" style="animation-delay:${Math.min(index * 18, 180)}ms">
            <div>
              <div class="email">${escapeHtml(item.email)}</div>
              <div class="meta">
                <span class="pill">${escapeHtml(item.subscription_type || 'null')}</span>
                <span class="pill">${escapeHtml(item.status || 'null')}</span>
                <span class="pill">${escapeHtml(item.source || 'null')}</span>
                <span class="pill ${rtClass}">RT · ${item.has_refresh_token ? '有' : '无'}</span>
                <span class="pill ${sessionClass}">Session · ${item.has_session ? '有' : '无'}</span>
              </div>
            </div>
            <div class="updated">${escapeHtml(fmtDate(item.updated_at))}</div>
          </article>
        `;
      }).join('');
      list.querySelectorAll('.account-card').forEach((node) => {
        node.addEventListener('click', () => selectAccount(Number(node.dataset.id)));
      });
    }

    async function loadAccounts() {
      const params = new URLSearchParams({
        search: $('search').value.trim(),
        plan: $('planFilter').value || 'all',
        status: $('statusFilter').value || 'all',
      });
      const data = await request(`/api/accounts?${params.toString()}`);
      state.items = data.items || [];
      state.stats = data.stats || {};
      renderStats(state.stats);
      renderFilters(state.stats);
      renderList(state.items);
    }

    function setTab(name) {
      document.querySelectorAll('.tab').forEach((node) => node.classList.toggle('is-active', node.dataset.tab === name));
      document.querySelectorAll('.tab-panel').forEach((node) => node.classList.toggle('is-hidden', node.dataset.panel !== name));
    }

    async function selectAccount(id) {
      const data = await request(`/api/accounts/${id}`);
      const item = data.item;
      setTab('form');
      state.selectedId = item.id;
      $('accountId').value = item.id;
      $('email').value = item.email || '';
      $('password').value = item.password === 'null' ? '' : item.password || '';
      $('subscriptionType').value = item.subscription_type === 'null' ? '' : item.subscription_type || '';
      $('refreshToken').value = item.refresh_token === 'null' ? '' : item.refresh_token || '';
      $('status').value = item.status === 'null' ? 'active' : item.status || 'active';
      $('source').value = item.source === 'null' ? 'manual' : item.source || 'manual';
      $('sessionJson').value = item.session_json === 'null' ? '' : prettyJson(item.session_json || '');
      $('opEmail').value = item.email || '';
      $('opPassword').value = item.password === 'null' ? '' : item.password || '';
      $('editorTitle').textContent = item.email || '编辑账号';
      $('editorHint').textContent = `创建 ${fmtDate(item.created_at)} · 更新 ${fmtDate(item.updated_at)}`;
      renderList(state.items);
    }

    function prettyJson(value) {
      if (!hasValue(value)) return '';
      try { return JSON.stringify(JSON.parse(value), null, 2); }
      catch { return value; }
    }

    function formPayload() {
      return {
        email: $('email').value.trim(),
        password: $('password').value,
        subscription_type: $('subscriptionType').value.trim() || 'null',
        refresh_token: $('refreshToken').value.trim() || 'null',
        session_json: $('sessionJson').value.trim() || 'null',
        status: $('status').value.trim() || 'active',
        source: $('source').value.trim() || 'manual',
      };
    }

    function resetForm() {
      state.selectedId = null;
      $('accountForm').reset();
      $('accountId').value = '';
      $('status').value = 'active';
      $('source').value = 'manual';
      $('opEmail').value = '';
      $('opPassword').value = '';
      $('editorTitle').textContent = '新建账号';
      $('editorHint').textContent = '保存后写入数据库';
      setTab('form');
      renderList(state.items);
    }

    async function saveForm(event) {
      event.preventDefault();
      const id = $('accountId').value;
      const payload = formPayload();
      const url = id ? `/api/accounts/${id}` : '/api/accounts';
      const method = id ? 'PUT' : 'POST';
      const data = await request(url, { method, body: JSON.stringify(payload) });
      toast('账号已保存，并已同步导出 TXT');
      await loadAccounts();
      await selectAccount(data.item.id);
    }

    async function deleteSelected() {
      const id = $('accountId').value;
      if (!id) return toast('当前没有选中账号');
      if (!confirm('确认删除这个账号记录？此操作会同步导出 accounts.txt。')) return;
      await request(`/api/accounts/${id}`, { method: 'DELETE' });
      toast('账号已删除');
      resetForm();
      await loadAccounts();
    }

    async function copyText(value, message) {
      if (!hasValue(value)) return toast('没有可复制内容');
      await navigator.clipboard.writeText(value);
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

    function jobPayload() {
      const form = formPayload();
      const email = $('opEmail').value.trim() || form.email;
      const password = $('opPassword').value || form.password;
      return {
        mode: $('opMode').value,
        email,
        password,
        account_id: $('opMode').value === 'register' ? null : ($('accountId').value ? Number($('accountId').value) : null),
        generate_email: $('opGenerateEmail').checked,
        generate_password: $('opGeneratePassword').checked,
        create_checkout: $('opCreateCheckout').checked,
      };
    }

    async function startJob() {
      setTab('ops');
      const data = await request('/api/ops/jobs', { method: 'POST', body: JSON.stringify(jobPayload()) });
      state.currentJobId = data.job.id;
      renderJob(data.job);
      toast('任务已启动');
      startJobPolling();
    }

    function startJobPolling() {
      clearInterval(state.jobTimer);
      state.jobTimer = setInterval(() => pollJob().catch((err) => toast(err.message)), 1100);
    }

    async function pollJob() {
      if (!state.currentJobId) return;
      const data = await request(`/api/ops/jobs/${state.currentJobId}`);
      renderJob(data.job);
      if (['succeeded', 'failed'].includes(data.job.status)) {
        clearInterval(state.jobTimer);
        await loadAccounts();
      }
    }

    function renderJob(job) {
      const labels = { pending: '等待中', running: '运行中', waiting: '等待验证码', succeeded: '已完成', failed: '失败' };
      const badge = $('jobState');
      badge.textContent = labels[job.status] || job.status;
      badge.dataset.status = job.status || 'idle';
      $('startJobBtn').disabled = ['pending', 'running', 'waiting'].includes(job.status);
      if (job.status === 'waiting') setTab('ops');
      const lines = (job.logs || []).join('\n');
      const result = job.result && Object.keys(job.result).length ? `\n\n[结果] ${JSON.stringify(job.result, null, 2)}` : '';
      const error = job.error ? `\n\n[错误] ${job.error}` : '';
      $('jobLog').textContent = lines || '任务日志会显示在这里。';
      $('jobLog').textContent += result + error;
      $('jobLog').scrollTop = $('jobLog').scrollHeight;
      $('promptBox').classList.toggle('show', job.status === 'waiting');
      if (job.status === 'waiting') {
        $('promptInput').placeholder = job.prompt || '输入验证码后继续';
        $('promptInput').focus();
      }
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
      renderJob(data.job);
      startJobPolling();
    }

    let searchTimer = null;
    $('search').addEventListener('input', () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => loadAccounts().catch((err) => toast(err.message)), 180);
    });
    $('planFilter').addEventListener('change', () => loadAccounts().catch((err) => toast(err.message)));
    $('statusFilter').addEventListener('change', () => loadAccounts().catch((err) => toast(err.message)));
    $('accountForm').addEventListener('submit', (event) => saveForm(event).catch((err) => toast(err.message)));
    $('newBtn').addEventListener('click', resetForm);
    $('deleteBtn').addEventListener('click', () => deleteSelected().catch((err) => toast(err.message)));
    $('copyLineBtn').addEventListener('click', () => copyText(compactLine(), '账号行已复制'));
    $('formatSessionBtn').addEventListener('click', () => { $('sessionJson').value = prettyJson($('sessionJson').value); });
    $('startJobBtn').addEventListener('click', () => startJob().catch((err) => toast(err.message)));
    $('submitPromptBtn').addEventListener('click', () => submitPrompt().catch((err) => toast(err.message)));
    $('promptInput').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') submitPrompt().catch((err) => toast(err.message));
    });
    $('syncBtn').addEventListener('click', async () => { await request('/api/sync', { method: 'POST' }); toast('已从本地文件同步导入'); await loadAccounts(); });
    $('exportBtn').addEventListener('click', async () => { const data = await request('/api/export', { method: 'POST' }); toast(`已导出到 ${data.path}`); });
    document.querySelectorAll('[data-copy]').forEach((button) => {
      button.addEventListener('click', () => {
        const target = button.dataset.copy === 'password' ? $('password') : $('refreshToken');
        copyText(target.value, '内容已复制').catch((err) => toast(err.message));
      });
    });
    document.querySelectorAll('.tab').forEach((node) => {
      node.addEventListener('click', () => setTab(node.dataset.tab));
    });

    resetForm();
    loadAccounts().catch((err) => toast(err.message));
  </script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
