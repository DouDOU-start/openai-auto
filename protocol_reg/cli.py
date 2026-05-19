from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import webbrowser

from .flow import RegisterFlow
from .settings import Settings
from .storage import save_account, save_credentials_txt, save_login_session, try_load_login_session
from .utils import make_password


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
        default="register",
        help="运行模式：register 注册新账号，login 仅登录保存会话，authorize 单独授权",
    )
    parser.add_argument("--proxy", default="", help="注册代理，例如 http://127.0.0.1:7897")
    parser.add_argument("--output", default=str(repo_root / "data" / "accounts.txt"), help="注册账号 TXT 输出路径")
    parser.add_argument("--token-output", default=str(repo_root / "data" / "tokens.jsonl"), help="授权 token JSONL 输出路径")
    parser.add_argument("--session-file", default=str(repo_root / "data" / "sessions.json"), help="登录会话 JSON 路径")
    parser.add_argument("--checkout-output", default=str(repo_root / "data" / "checkout_urls.jsonl"), help="支付链接 JSONL 输出路径")
    parser.add_argument("--license-file", default=str(default_license) if default_license else "", help="auth_core 授权文件路径")
    parser.add_argument("--login-delay", type=int, default=20, help="注册成功后等待多少秒再获取 ChatGPT session")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP 超时时间，单位秒")
    parser.add_argument("--no-ssl-verify", action="store_true", help="关闭 TLS 证书校验")
    parser.add_argument("--open-checkout", action="store_true", help="拿到支付链接后自动用系统浏览器打开")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    repo_root = _repo_root()
    token_output = Path(args.token_output).resolve()
    checkout_output = Path(args.checkout_output).resolve()
    settings = Settings(
        project_root=_default_project_root(repo_root).resolve(),
        proxy=args.proxy,
        output=Path(args.output).resolve(),
        session_file=Path(args.session_file).resolve(),
        license_file=Path(args.license_file).resolve() if args.license_file else None,
        login_delay=max(0, args.login_delay),
        timeout=max(1, args.timeout),
        ssl_verify=not args.no_ssl_verify,
    )

    email = input("请输入邮箱: ").strip()
    if not email:
        raise SystemExit("[错误] 邮箱不能为空")
    if args.mode == "register":
        password = input("请输入密码，留空自动生成: ").strip() or make_password()
        print(f"[注册] 使用密码: {password}")
    elif args.mode == "login":
        password = input("请输入已有账号密码: ").strip()
        if not password:
            raise SystemExit("[错误] 登录模式必须输入已有账号密码")
    else:
        password = ""

    flow = RegisterFlow(settings, prompt=input)
    account_saved = False
    try:
        if args.mode == "register":
            token_data = flow.run(email, password)
            save_credentials_txt(settings.output, email, password)
            account_saved = True
            _print_chatgpt_session(token_data.get("chatgpt_session"))
            checkout = flow.create_plus_trial_checkout(
                flow.http.session,
                token_data.get("chatgpt_session") if isinstance(token_data.get("chatgpt_session"), dict) else {},
            )
            _handle_checkout(checkout, checkout_output, args.open_checkout)
        elif args.mode == "login":
            session_data = flow.login(email, password)
            save_login_session(settings.session_file, email, password, session_data)
            _print_chatgpt_session(session_data.get("chatgpt_session"))
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
    except KeyboardInterrupt:
        raise SystemExit("\n[中断] 用户取消")
    except Exception as exc:
        if args.mode == "register" and account_saved:
            print(f"[完成] 账号密码已写入: {settings.output}")
        raise SystemExit(f"[错误] {exc}") from exc
    finally:
        flow.close()

    if args.mode == "login":
        print(f"[完成] 登录会话已保存: {settings.session_file}")
        return
    action = "注册" if args.mode == "register" else "授权"
    print(f"[完成] {action}成功: {token_data.get('email') or email}")
    if args.mode == "register":
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
