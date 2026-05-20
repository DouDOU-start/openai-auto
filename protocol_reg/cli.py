from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import string
import subprocess
import sys
import webbrowser

import yaml

from .config import config_template, load_app_config
from .flow import RegisterFlow
from .settings import Settings
from .storage import (
    save_account,
    save_credentials_rt_txt,
    save_credentials_txt,
    save_login_session,
    try_load_login_session,
)
from .utils import make_password


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
    parser.add_argument("--proxy", default="", help="注册代理，例如 http://127.0.0.1:7897")
    parser.add_argument("--output", default=str(repo_root / "data" / "accounts.txt"), help="注册账号 TXT 输出路径")
    parser.add_argument("--token-output", default=str(repo_root / "data" / "tokens.jsonl"), help="授权 token JSONL 输出路径")
    # Always export a compact rt file by default; keep flag for overrides.
    # Intentionally hidden from help: this is a core artifact we always produce.
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
    parser.add_argument("--open-checkout", dest="open_checkout", action="store_true", default=True, help="拿到支付链接后自动用系统浏览器打开，默认开启")
    parser.add_argument("--no-open-checkout", dest="open_checkout", action="store_false", help="只保存支付长链接，不自动打开浏览器")
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

    index = 0
    line_count = len(_MODE_OPTIONS) + 1

    def render(first: bool = False) -> None:
        if not first:
            sys.stdout.write(f"\x1b[{line_count}A")
        sys.stdout.write("\x1b[J")
        sys.stdout.write("请选择运行模式（↑/↓ 切换，Enter 确认，1/2/3 直接选择）：\n")
        for option_index, (value, label) in enumerate(_MODE_OPTIONS):
            marker = ">" if option_index == index else " "
            sys.stdout.write(f" {marker} {option_index + 1}. {value:<9} {label}\n")
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
                return _MODE_OPTIONS[index][0]
            if key == "\x03":
                raise KeyboardInterrupt
            if key in {"1", "2", "3"}:
                chosen = int(key) - 1
                sys.stdout.write("\n")
                sys.stdout.flush()
                return _MODE_OPTIONS[chosen][0]
            if key in ("\x1b[A", "\x1bOA"):
                index = (index - 1) % len(_MODE_OPTIONS)
                render()
            elif key in ("\x1b[B", "\x1bOB"):
                index = (index + 1) % len(_MODE_OPTIONS)
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
    if not suffixes:
        raise SystemExit("[错误] 邮箱留空随机生成时，必须先在配置文件设置 email_suffixes")
    alphabet = string.ascii_lowercase + string.digits
    for _ in range(1000):
        suffix = secrets.choice(suffixes)
        local = "oa" + "".join(secrets.choice(alphabet) for _ in range(12))
        email = f"{local}@{suffix}".lower()
        if email not in existing_emails:
            return email
    raise SystemExit("[错误] 随机邮箱生成失败：配置后缀下的候选邮箱均与已有记录冲突")


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

    # 优先级：命令行参数（非空 / >0）> 环境变量 > 配置文件。
    proxy = (
        str(args.proxy or "").strip()
        or os.environ.get("PROTOCOL_REG_PROXY", "").strip()
        or cfg.proxy
    )
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
    settings = Settings(
        project_root=_default_project_root(repo_root).resolve(),
        proxy=str(proxy or "").strip(),
        output=Path(args.output).resolve(),
        session_file=Path(args.session_file).resolve(),
        license_file=Path(args.license_file).resolve() if args.license_file else None,
        login_delay=max(0, args.login_delay),
        timeout=max(1, args.timeout),
        ssl_verify=not args.no_ssl_verify,

        email_code_api_base=str(email_code_api or "").strip(),
        email_code_api_key=str(email_code_key or "").strip(),
        email_code_sender_suffix=str(email_code_sender_suffix or "openai.com").strip() or "openai.com",
        email_code_poll_interval=max(0.5, float(email_code_poll)),
        email_code_timeout=max(5, int(email_code_timeout)),
    )

    existing_emails = _collect_existing_emails(settings.output, rt_output, token_output, settings.session_file)
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
            save_credentials_txt(settings.output, email, password)
            save_credentials_rt_txt(rt_output, email, password, str(token_data.get("refresh_token") or ""))
            account_saved = True
            _print_chatgpt_session(token_data.get("chatgpt_session"))
            checkout = flow.create_plus_trial_checkout(
                flow.http.session,
                token_data.get("chatgpt_session") if isinstance(token_data.get("chatgpt_session"), dict) else {},
            )
            _handle_checkout(checkout, checkout_output, args.open_checkout)
        elif mode == "login":
            session_data = flow.login(email, password)
            save_login_session(settings.session_file, email, password, session_data)
            _print_chatgpt_session(session_data.get("chatgpt_session"))
            if not args.no_checkout:
                _handle_checkout(session_data.get("plus_trial_checkout"), checkout_output, args.open_checkout)
            token_data = {}
        else:
            session_data = try_load_login_session(settings.session_file, email)
            if session_data is None:
                print("[授权] 未找到可用登录会话，改为输入密码并即时登录")
                password = input("请输入已有账号密码: ").strip()
                if not password:
                    raise SystemExit("[错误] 单独授权必须有登录会话或输入已有账号密码")
                session_data = flow.login(email, password)
                save_login_session(settings.session_file, email, password, session_data)
            else:
                password = str(session_data.get("password") or "")
            token_data = flow.authorize_from_session(email, session_data)
            save_account(token_output, email, password, token_data)
            save_credentials_rt_txt(rt_output, email, password, str(token_data.get("refresh_token") or ""))
    except KeyboardInterrupt:
        raise SystemExit("\n[中断] 用户取消")
    except Exception as exc:
        if mode == "register" and account_saved:
            print(f"[完成] 账号密码已写入: {settings.output}")
        raise SystemExit(f"[错误] {exc}") from exc
    finally:
        flow.close()

    if mode == "login":
        print(f"[完成] 登录会话已保存: {settings.session_file}")
        return
    action = "注册" if mode == "register" else "授权"
    print(f"[完成] {action}成功: {token_data.get('email') or email}")
    if mode == "register":
        print(f"[完成] 账号密码已写入: {settings.output}")
    else:
        print(f"[完成] 授权数据已写入: {token_output}")
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


def _handle_checkout(checkout: object, output: Path, open_checkout: bool) -> None:
    _print_plus_trial_checkout(checkout)
    long_url = _checkout_long_url(checkout)
    if not long_url:
        raise RuntimeError("未获取到支付长链接，停止处理")
    _save_checkout_url(long_url, checkout, output)
    if open_checkout:
        _open_checkout_url(long_url)


def _checkout_long_url(checkout: object) -> str:
    if not isinstance(checkout, dict):
        return ""
    for key in ("long_url", "hosted_url", "openai_payurl"):
        url = str(checkout.get(key) or "").strip()
        if url:
            return url
    return ""


def _save_checkout_url(long_url: str, checkout: object, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "long_url": long_url,
        "status_code": checkout.get("status_code") if isinstance(checkout, dict) else None,
    }
    with output.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    print(f"[Plus] 支付链接已写入: {output}")


def _open_checkout_url(long_url: str) -> None:
    print(f"[Plus] 正在打开支付长链接: {long_url}")
    if _open_with_system_browser(long_url):
        return
    try:
        if webbrowser.open(long_url):
            return
    except Exception:
        pass
    print("[Plus] 自动打开浏览器失败，请手动复制上方支付链接")


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
        executable = command[0]
        if executable not in {"cmd.exe"} and shutil.which(executable) is None:
            continue
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            continue
        if completed.returncode == 0:
            return True
    return False


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except Exception:
        return False
