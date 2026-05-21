from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import webbrowser

import yaml

from .config import config_template, load_app_config
from .flow import RegisterFlow
from .proxy_pool import pick_proxy_from_pool
from .settings import Settings, proxy_preview, resolve_proxy_pool
from .storage import (
    NULL_VALUE,
    load_account_records,
    merge_legacy_rt_txt,
    save_account,
    save_account_storage,
    save_login_session,
    sync_account_storage,
    try_load_login_session,
    update_account_checkout_url_db,
)
from .utils import make_password, make_random_email


_MODE_OPTIONS = (
    ("register", "注册新账号"),
    ("login", "仅登录保存会话"),
    ("authorize", "使用已保存会话授权"),
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _default_license_file(repo_root: Path) -> Path | None:
    for candidate in (repo_root / "config" / "wenfxl.license", repo_root / "wenfxl.license"):
        if candidate.exists():
            return candidate
    return None


def _default_project_root(repo_root: Path) -> Path:
    return repo_root


def build_parser() -> argparse.ArgumentParser:
    repo_root = _repo_root()
    default_license = _default_license_file(repo_root)
    parser = argparse.ArgumentParser(description="交互式协议注册/授权 CLI")
    parser.add_argument(
        "--mode",
        choices=("register", "login", "authorize"),
        default=None,
        help="运行模式：register 注册新账号，login 仅登录保存会话，authorize 单独授权；不传时用上下键交互式选择",
    )
    parser.add_argument(
        "--config",
        default=str(repo_root / "config" / "protocol-reg.yaml"),
        help="配置文件路径（YAML），默认 config/protocol-reg.yaml",
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="生成默认配置文件到 --config 路径后退出",
    )
    parser.add_argument("--proxy", default="", help="注册代理；多个代理可用逗号分隔，例如 http://127.0.0.1:7897,http://127.0.0.1:7898")
    parser.add_argument("--output", default=str(repo_root / "data" / "accounts.txt"), help="注册账号 TXT 输出路径")
    parser.add_argument("--token-output", default=str(repo_root / "data" / "tokens.jsonl"), help="授权 token JSONL 输出路径")
    # 兼容旧 accounts_rt.txt：启动时自动合并到 accounts.txt，不再单独写入。
    parser.add_argument(
        "--rt-output",
        default=str(repo_root / "data" / "accounts_rt.txt"),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--session-file", default=str(repo_root / "data" / "sessions.json"), help="登录会话 JSON 路径")
    parser.add_argument("--checkout-output", default=str(repo_root / "data" / "checkout_urls.jsonl"), help="支付链接 JSONL 输出路径")
    parser.add_argument("--license-file", default=str(default_license) if default_license else "", help="auth_core 授权文件路径")
    parser.add_argument("--login-delay", type=int, default=20, help="注册成功后等待多少秒再获取 ChatGPT session")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP 超时时间，单位秒")
    parser.add_argument("--no-ssl-verify", action="store_true", help="关闭 TLS 证书校验")

    # cloudflare-email email-code-api.md
    # Config file is the primary source; flags/env vars override.
    parser.add_argument("--email-code-api", default="", help="cloudflare-email 验证码 API base，例如 https://mail.example.com")
    parser.add_argument("--email-code-key", default="", help="cloudflare-email ADMIN_API_KEY（Authorization: Bearer ...）")
    parser.add_argument("--email-code-sender-suffix", default="", help="验证码邮件发件人域名后缀，默认 openai.com")
    parser.add_argument("--email-code-timeout", type=int, default=0, help="等待邮箱验证码超时秒数，默认 120")
    parser.add_argument("--email-code-poll", type=float, default=0.0, help="邮箱验证码轮询间隔秒数，默认 2.0")
    parser.add_argument(
        "--no-checkout",
        action="store_true",
        help="跳过 Plus/Stripe checkout 链路（仅登录/授权，不生成支付链接）",
    )
    parser.add_argument("--open-checkout", dest="open_checkout", action="store_true", default=False, help="拿到支付链接后自动用系统浏览器打开，默认关闭")
    parser.add_argument("--no-open-checkout", dest="open_checkout", action="store_false", help="只保存并显示支付长链接，不自动打开浏览器")
    parser.add_argument("--incognito-checkout", action="store_true", help="自动打开支付链接时优先使用浏览器无痕模式")
    return parser


def _resolve_mode(mode: str | None, prompt=input) -> str:
    if mode:
        return mode

    selected = _select_mode_with_keys()
    if selected:
        return selected

    return _prompt_mode_number(prompt)


def _prompt_mode_number(prompt=input) -> str:
    choices = {
        "1": "register",
        "2": "login",
        "3": "authorize",
    }
    print("请选择运行模式：")
    for index, (value, label) in enumerate(_MODE_OPTIONS, 1):
        print(f"  {index}. {value:<9} {label}")
    while True:
        try:
            raw = prompt("请输入 1/2/3: ").strip()
        except EOFError as exc:
            raise SystemExit("[错误] 未指定 --mode 且无法读取交互输入") from exc
        selected = choices.get(raw)
        if selected:
            return selected
        print("[错误] 无效运行模式，请重新输入")


def _select_mode_with_keys() -> str | None:
    options = [(value, f"{value:<9} {label}", "") for value, label in _MODE_OPTIONS]
    return _select_with_keys("请选择运行模式", options)


def _select_with_keys(title: str, options: list[tuple[str, str, str]]) -> str | None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return None
    try:
        import select
        import termios
        import tty
    except ImportError:
        return None

    fd = sys.stdin.fileno()
    try:
        old_settings = termios.tcgetattr(fd)
    except termios.error:
        return None

    if not options:
        return None

    index = 0
    line_count = len(options) + 1

    def render(first: bool = False) -> None:
        if not first:
            sys.stdout.write(f"\x1b[{line_count}A")
        sys.stdout.write("\x1b[J")
        sys.stdout.write(f"{title}（↑/↓ 切换，Enter 确认，数字直接选择）：\n")
        for option_index, (_, label, detail) in enumerate(options):
            marker = ">" if option_index == index else " "
            suffix = f" {detail}" if detail else ""
            sys.stdout.write(f" {marker} {option_index + 1}. {label}{suffix}\n")
        sys.stdout.flush()

    def read_key() -> str:
        key = os.read(fd, 1).decode("utf-8", errors="ignore")
        if key != "\x1b":
            return key
        sequence = key
        while select.select([fd], [], [], 0.1)[0]:
            sequence += os.read(fd, 1).decode("utf-8", errors="ignore")
            if len(sequence) >= 3:
                break
        return sequence

    try:
        tty.setcbreak(fd)
        render(first=True)
        while True:
            key = read_key()
            if key in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return options[index][0]
            if key == "\x03":
                raise KeyboardInterrupt
            if key.isdigit() and key != "0":
                chosen = int(key) - 1
                if chosen >= len(options):
                    continue
                sys.stdout.write("\n")
                sys.stdout.flush()
                return options[chosen][0]
            if key in ("\x1b[A", "\x1bOA"):
                index = (index - 1) % len(options)
                render()
            elif key in ("\x1b[B", "\x1bOB"):
                index = (index + 1) % len(options)
                render()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _collect_existing_emails(*paths: Path) -> set[str]:
    emails: set[str] = set()
    for path in paths:
        if not path.exists() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _EMAIL_RE.findall(text):
            emails.add(match.lower())
    return emails


def _random_email(suffixes: tuple[str, ...], existing_emails: set[str]) -> str:
    try:
        return make_random_email(suffixes, existing_emails)
    except ValueError as exc:
        raise SystemExit(f"[错误] {exc}") from exc


def _choose_authorize_account(accounts_path: Path) -> tuple[str, str] | None:
    accounts = [account for account in load_account_records(accounts_path) if _usable_saved_account(account)]
    if not accounts:
        return None

    options: list[tuple[str, str, str]] = []
    for index, account in enumerate(accounts):
        email = account["email"]
        plan = _display_field(account.get("subscription_type", ""))
        rt_state = "有 RT" if _has_value(account.get("refresh_token", "")) else "无 RT"
        options.append((str(index), email, f"[{plan} / {rt_state}]"))
    options.append(("manual", "手动输入邮箱", ""))

    selected = _select_with_keys("请选择要授权的账号", options)
    if selected is None:
        return _prompt_authorize_account_number(accounts)
    if selected == "manual":
        return None
    account = accounts[int(selected)]
    print(f"[授权] 使用已保存账号: {account['email']}")
    return account["email"], account["password"]


def _prompt_authorize_account_number(accounts: list[dict[str, str]], prompt=input) -> tuple[str, str] | None:
    print("请选择要授权的账号：")
    for index, account in enumerate(accounts, 1):
        plan = _display_field(account.get("subscription_type", ""))
        rt_state = "有 RT" if _has_value(account.get("refresh_token", "")) else "无 RT"
        print(f"  {index}. {account['email']} [{plan} / {rt_state}]")
    print(f"  {len(accounts) + 1}. 手动输入邮箱")
    while True:
        raw = prompt(f"请输入 1/{len(accounts) + 1}: ").strip()
        if not raw.isdigit():
            print("[错误] 无效选择，请重新输入")
            continue
        index = int(raw)
        if 1 <= index <= len(accounts):
            account = accounts[index - 1]
            print(f"[授权] 使用已保存账号: {account['email']}")
            return account["email"], account["password"]
        if index == len(accounts) + 1:
            return None
        print("[错误] 无效选择，请重新输入")


def _usable_saved_account(account: dict[str, str]) -> bool:
    return _has_value(account.get("email", "")) and _has_value(account.get("password", ""))


def _has_value(value: object) -> bool:
    text = str(value or "").strip()
    return bool(text and text.lower() != NULL_VALUE)


def _display_field(value: object) -> str:
    return str(value).strip() if _has_value(value) else NULL_VALUE


def main() -> None:
    args = build_parser().parse_args()
    repo_root = _repo_root()

    config_path = Path(args.config).resolve()
    if args.init_config:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(
            yaml.safe_dump(config_template(), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        print(f"[完成] 已生成配置文件: {config_path}")
        return

    cfg = load_app_config(config_path)
    mode = _resolve_mode(args.mode)
    token_output = Path(args.token_output).resolve()
    rt_output = Path(args.rt_output).resolve()
    checkout_output = Path(args.checkout_output).resolve()
    accounts_output = Path(args.output).resolve()
    session_file = Path(args.session_file).resolve()
    merge_legacy_rt_txt(accounts_output, rt_output)

    # 优先级：命令行参数（非空 / >0）> 环境变量 > 配置文件。
    proxy_pool = resolve_proxy_pool(
        str(args.proxy or "").strip(),
        os.environ.get("PROTOCOL_REG_PROXIES", ""),
        os.environ.get("PROTOCOL_REG_PROXY", ""),
        getattr(cfg, "proxies", ()),
        getattr(cfg, "proxy", ""),
    )
    proxy = pick_proxy_from_pool(proxy_pool)
    if len(proxy_pool) > 1:
        print(f"[代理] 已配置 {len(proxy_pool)} 个代理，本次 CLI 轮询使用: {proxy_preview(proxy)}")
    elif proxy:
        print(f"[代理] 使用代理: {proxy_preview(proxy)}")
    email_code_api = (
        str(args.email_code_api or "").strip()
        or os.environ.get("EMAIL_CODE_API", "").strip()
        or cfg.email_code_api
    )
    email_code_key = (
        str(args.email_code_key or "").strip()
        or os.environ.get("EMAIL_CODE_API_KEY", "").strip()
        or cfg.email_code_key
    )
    email_code_sender_suffix = (
        str(args.email_code_sender_suffix or "").strip()
        or os.environ.get("EMAIL_CODE_SENDER_SUFFIX", "").strip()
        or cfg.email_code_sender_suffix
        or "openai.com"
    )
    email_code_timeout = (
        int(args.email_code_timeout)
        if int(args.email_code_timeout) > 0
        else int(os.environ.get("EMAIL_CODE_TIMEOUT", "0") or 0) or cfg.email_code_timeout
    )
    email_code_poll = (
        float(args.email_code_poll)
        if float(args.email_code_poll) > 0
        else float(os.environ.get("EMAIL_CODE_POLL", "0") or 0) or cfg.email_code_poll
    )
    otp_max_retries = _positive_int(os.environ.get("EMAIL_CODE_MAX_OTP_RETRIES"), cfg.otp_max_retries)
    otp_poll_max_attempts = _positive_int(
        os.environ.get("EMAIL_CODE_OTP_POLL_MAX_ATTEMPTS"),
        cfg.otp_poll_max_attempts,
    )
    use_proxy_for_email = _boolish(os.environ.get("EMAIL_CODE_USE_PROXY"), cfg.use_proxy_for_email)
    settings = Settings(
        project_root=_default_project_root(repo_root).resolve(),
        proxy=str(proxy or "").strip(),
        output=accounts_output,
        session_file=session_file,
        license_file=Path(args.license_file).resolve() if args.license_file else None,
        login_delay=max(0, args.login_delay),
        timeout=max(1, args.timeout),
        ssl_verify=not args.no_ssl_verify,

        email_code_api_base=str(email_code_api or "").strip(),
        email_code_api_key=str(email_code_key or "").strip(),
        email_code_sender_suffix=str(email_code_sender_suffix or "openai.com").strip() or "openai.com",
        email_code_poll_interval=max(0.5, float(email_code_poll)),
        email_code_timeout=max(5, int(email_code_timeout)),
        otp_max_retries=max(1, int(otp_max_retries)),
        otp_poll_max_attempts=max(1, int(otp_poll_max_attempts)),
        use_proxy_for_email=bool(use_proxy_for_email),
    )
    sync_account_storage(settings.output, settings.session_file)

    existing_emails = _collect_existing_emails(settings.output, rt_output, token_output, settings.session_file)
    selected_account = _choose_authorize_account(settings.output) if mode == "authorize" else None
    if selected_account is not None:
        email, password = selected_account
    else:
        email_prompt = "请输入邮箱，留空随机生成: " if mode == "register" else "请输入邮箱: "
        email = input(email_prompt).strip().lower()
        if mode == "register" and not email:
            email = _random_email(cfg.email_suffixes, existing_emails)
            print(f"[注册] 随机生成邮箱: {email}")
        if not email:
            raise SystemExit("[错误] 邮箱不能为空")
        if mode == "register" and email in existing_emails:
            raise SystemExit(f"[错误] 邮箱已存在于本地记录，避免重复注册: {email}")
        if mode == "register":
            password = input("请输入密码，留空自动生成: ").strip() or make_password()
            print(f"[注册] 使用密码: {password}")
        elif mode == "login":
            password = input("请输入已有账号密码: ").strip()
            if not password:
                raise SystemExit("[错误] 登录模式必须输入已有账号密码")
        else:
            password = ""

    flow = RegisterFlow(settings, prompt=input)
    account_saved = False
    try:
        if mode == "register":
            token_data = flow.run(email, password)
            save_account_storage(settings.output, email, password, token_data, source="register")
            account_saved = True
            _print_chatgpt_session(token_data.get("chatgpt_session"))
            checkout = flow.create_plus_trial_checkout(
                flow.http.session,
                token_data.get("chatgpt_session") if isinstance(token_data.get("chatgpt_session"), dict) else {},
            )
            _handle_checkout(checkout, checkout_output, args.open_checkout, args.incognito_checkout, email=email)
        elif mode == "login":
            session_data = flow.login(email, password, create_checkout=not args.no_checkout)
            save_login_session(settings.session_file, email, password, session_data)
            save_account_storage(settings.output, email, password, session_data, source="login")
            _print_chatgpt_session(session_data.get("chatgpt_session"))
            if not args.no_checkout:
                _handle_checkout(
                    session_data.get("plus_trial_checkout"),
                    checkout_output,
                    args.open_checkout,
                    args.incognito_checkout,
                    email=email,
                )
            token_data = {}
        else:
            session_data = try_load_login_session(settings.session_file, email)
            if session_data is None:
                print("[授权] 未找到可用登录会话，改为使用密码即时登录")
                if not password:
                    password = input("请输入已有账号密码: ").strip()
                if not password:
                    raise SystemExit("[错误] 单独授权必须有登录会话或输入已有账号密码")
                session_data = flow.login(email, password, create_checkout=False)
                save_login_session(settings.session_file, email, password, session_data)
                save_account_storage(settings.output, email, password, session_data, source="login")
            else:
                password = str(session_data.get("password") or "")
            token_data = flow.authorize_from_session(email, session_data)
            save_account(token_output, email, password, token_data)
            save_account_storage(settings.output, email, password, token_data, source="authorize")
    except KeyboardInterrupt:
        raise SystemExit("\n[中断] 用户取消")
    except Exception as exc:
        if mode == "register" and account_saved:
            print(f"[完成] 账号数据已写入: {settings.output}")
        raise SystemExit(f"[错误] {exc}") from exc
    finally:
        flow.close()

    if mode == "login":
        print(f"[完成] 登录会话已保存: {settings.session_file}")
        return
    action = "注册" if mode == "register" else "授权"
    print(f"[完成] {action}成功: {token_data.get('email') or email}")
    if mode == "register":
        print(f"[完成] 账号数据已写入: {settings.output}")
    else:
        print(f"[完成] 授权数据已写入: {token_output}")
        print(f"[完成] 账号数据已写入: {settings.output}")
        print(f"[完成] 登录会话文件: {settings.session_file}")


def _print_chatgpt_session(chatgpt_session: object) -> None:
    print("[身份] https://chatgpt.com/api/auth/session 返回:")
    if not isinstance(chatgpt_session, dict):
        print(json.dumps(chatgpt_session, ensure_ascii=False, indent=2))
        return
    if "data" in chatgpt_session:
        print(json.dumps(chatgpt_session["data"], ensure_ascii=False, indent=2))
        return
    if "text" in chatgpt_session:
        print(str(chatgpt_session["text"]))
        return
    print(json.dumps(chatgpt_session, ensure_ascii=False, indent=2))


def _print_plus_trial_checkout(checkout: object) -> None:
    print("[Plus] 美区 0 刀试用 checkout 返回:")
    if not isinstance(checkout, dict):
        print(json.dumps(checkout, ensure_ascii=False, indent=2))
        return
    long_url = str(checkout.get("long_url") or "").strip()
    short_url = str(checkout.get("short_url") or "").strip()
    openai_payurl = str(checkout.get("openai_payurl") or "").strip()
    raw = checkout.get("data")
    if isinstance(raw, dict):
        checkout_session_id = raw.get("checkout_session_id")
        processor_entity = raw.get("processor_entity")
        if checkout_session_id:
            print(f"Checkout Session ID: {checkout_session_id}")
        if processor_entity:
            print(f"Processor Entity: {processor_entity}")
        print("Plan: ChatGPT Plus（US/USD，plus-1-month-free）")
    if long_url:
        print(f"支付长链接: {long_url}")
    if openai_payurl and openai_payurl != long_url:
        print(f"OpenAI Pay 长链接: {openai_payurl}")
    if short_url:
        print(f"ChatGPT 支付短链: {short_url}")
    if not long_url and not short_url:
        print(json.dumps(checkout, ensure_ascii=False, indent=2))
        return
    if raw is not None:
        print("[Plus] 原始响应:")
        print(json.dumps(raw, ensure_ascii=False, indent=2))


def _handle_checkout(
    checkout: object,
    output: Path,
    open_checkout: bool,
    incognito: bool = False,
    *,
    email: str = "",
) -> str:
    _print_plus_trial_checkout(checkout)
    long_url = _checkout_long_url(checkout)
    if not long_url:
        raise RuntimeError("未获取到支付长链接，停止处理")
    _save_checkout_url(long_url, checkout, output, email=email)
    if email:
        update_account_checkout_url_db(email, long_url)
    if open_checkout:
        _open_checkout_url(long_url, incognito=incognito)
    return long_url


def _checkout_long_url(checkout: object) -> str:
    if not isinstance(checkout, dict):
        return ""
    for key in ("long_url", "hosted_url", "openai_payurl"):
        url = str(checkout.get(key) or "").strip()
        if url:
            return url
    return ""


def _save_checkout_url(long_url: str, checkout: object, output: Path, *, email: str = "") -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "email": email.strip().lower() or None,
        "long_url": long_url,
        "status_code": checkout.get("status_code") if isinstance(checkout, dict) else None,
    }
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"[Plus] 支付链接已写入: {output}")


def _open_checkout_url(long_url: str, *, incognito: bool = False) -> None:
    mode = "无痕模式" if incognito else "默认浏览器"
    print(f"[Plus] 正在使用{mode}打开支付长链接: {long_url}")
    if incognito and _open_with_incognito_browser(long_url):
        return
    if incognito:
        print("[Plus] 无痕模式打开失败，回退到系统默认浏览器")
    if _open_with_system_browser(long_url):
        return
    try:
        if webbrowser.open(long_url):
            return
    except Exception:
        pass
    print("[Plus] 自动打开浏览器失败，请手动复制上方支付链接")


def _open_with_incognito_browser(url: str) -> bool:
    for command in _incognito_browser_commands(url):
        if not _command_available(command):
            continue
        if _run_open_command(command):
            return True
    return False


def _incognito_browser_commands(url: str) -> list[list[str]]:
    if _is_wsl():
        return [
            ["cmd.exe", "/c", "start", "", "msedge", "--inprivate", url],
            ["cmd.exe", "/c", "start", "", "chrome", "--incognito", url],
            ["cmd.exe", "/c", "start", "", "brave", "--incognito", url],
        ]
    commands = [
        ["google-chrome", "--incognito", url],
        ["google-chrome-stable", "--incognito", url],
        ["chromium", "--incognito", url],
        ["chromium-browser", "--incognito", url],
        ["brave-browser", "--incognito", url],
        ["microsoft-edge", "--inprivate", url],
        ["msedge", "--inprivate", url],
    ]
    if sys.platform == "darwin":
        commands = [
            ["open", "-na", "Google Chrome", "--args", "--incognito", url],
            ["open", "-na", "Microsoft Edge", "--args", "--inprivate", url],
            ["open", "-na", "Brave Browser", "--args", "--incognito", url],
        ]
    return commands


def _open_with_system_browser(url: str) -> bool:
    commands: list[list[str]] = []
    if _is_wsl():
        commands.extend([
            ["cmd.exe", "/c", "start", "", url],
            ["wslview", url],
        ])
    commands.extend([
        ["xdg-open", url],
        ["open", url],
    ])
    for command in commands:
        if not _command_available(command):
            continue
        if _run_open_command(command):
            return True
    return False


def _command_available(command: list[str]) -> bool:
    executable = command[0]
    return executable in {"cmd.exe"} or shutil.which(executable) is not None


def _run_open_command(command: list[str]) -> bool:
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except Exception:
        return False
    return completed.returncode == 0


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except Exception:
        return False


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
