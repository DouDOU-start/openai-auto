from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import urlopen


DEFAULT_CHECKOUT_USERSCRIPT = Path(__file__).with_name("checkout_auto_filler.user.js")
CHECKOUT_SCRIPT_MATCHES = (
    "https://www.paypal.com/*",
    "https://pay.openai.com/*",
    "https://checkout.stripe.com/*",
)
CHECKOUT_SCRIPT_CONNECTS = (
    "https://www.meiguodizhi.com/*",
    "https://mail-api.yuecheng.shop/*",
)


class BrowserLaunchError(RuntimeError):
    pass


class BrowserAutomationError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserLaunchResult:
    browser: str
    profile_dir: Path
    remote_debugging_port: int | None = None
    debugger_host: str = ""
    extension_dir: Path | None = None
    browser_proxy: str = ""
    automation_result: Any = None

    def to_public_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "browser": self.browser,
            "profile_dir": str(self.profile_dir),
            "fresh_profile": True,
        }
        if self.remote_debugging_port:
            data["remote_debugging_port"] = self.remote_debugging_port
        if self.debugger_host:
            data["debugger_host"] = self.debugger_host
        if self.extension_dir:
            data["extension_dir"] = str(self.extension_dir)
        if self.browser_proxy:
            data["browser_proxy"] = self.browser_proxy
        if self.automation_result is not None:
            data["automation_result"] = self.automation_result
        return data


@dataclass(frozen=True)
class _BrowserProxyConfig:
    server_arg: str
    preview: str
    username: str = ""
    password: str = ""


@dataclass(frozen=True)
class _BrowserCommand:
    name: str
    command: tuple[str, ...]
    wait_for_exit_code: bool = False


@dataclass(frozen=True)
class CheckoutSmsRuntimeConfig:
    numbers: tuple[dict[str, str], ...] = ()
    timeout_seconds: int = 180
    poll_seconds: float = 2.0
    lease_acquire_url: str = ""
    lease_release_url: str = ""
    lease_token: str = ""
    lease_wait_seconds: float = 25.0

    def to_public_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "numbers": [
                {
                    "phone": item.get("phone", ""),
                    "smsUrl": item.get("smsUrl", ""),
                    "label": item.get("label", ""),
                }
                for item in self.numbers
            ],
            "timeoutSeconds": max(5, int(self.timeout_seconds or 180)),
            "pollSeconds": max(1.0, float(self.poll_seconds or 2.0)),
        }
        if self.lease_acquire_url and self.lease_release_url and self.lease_token:
            data["lease"] = {
                "acquireUrl": self.lease_acquire_url,
                "releaseUrl": self.lease_release_url,
                "token": self.lease_token,
                "waitSeconds": max(1.0, float(self.lease_wait_seconds or 25.0)),
            }
        return data


def open_url_in_fresh_browser(
    url: str,
    *,
    profile_root: Path,
    automation_js: str = "",
    inject_auto_filler: bool = True,
    auto_filler_script: Path | None = None,
    browser_proxy: str = "",
    checkout_sms: object = None,
    wait_after_open_ms: int = 1500,
) -> BrowserLaunchResult:
    checkout_url = _validate_http_url(url)
    profile_root = Path(profile_root).resolve()
    profile_root.mkdir(parents=True, exist_ok=True)
    cleanup_old_browser_profiles(profile_root)
    profile_dir = _new_profile_dir(profile_root)
    _write_jp_profile_preferences(profile_dir)
    proxy_config = _browser_proxy_config(browser_proxy)
    checkout_sms_config = _normalize_checkout_sms_config(checkout_sms)
    extension_dir = _build_auto_filler_extension(
        profile_dir,
        auto_filler_script or DEFAULT_CHECKOUT_USERSCRIPT,
        proxy_config=proxy_config,
        checkout_sms=checkout_sms_config,
    ) if inject_auto_filler else None
    automation_script = str(automation_js or "").strip()
    debug_port = _free_port() if automation_script else None

    last_error = ""
    for command in _browser_commands(
        profile_dir,
        checkout_url,
        debug_port,
        extension_dir=extension_dir,
        proxy_config=proxy_config,
    ):
        try:
            if _launch_browser(command):
                automation_result: Any = None
                debugger_host = ""
                if automation_script and debug_port:
                    time.sleep(max(0, int(wait_after_open_ms or 0)) / 1000)
                    debugger_host, automation_result = execute_js_via_cdp(debug_port, automation_script)
                return BrowserLaunchResult(
                    browser=command.name,
                    profile_dir=profile_dir,
                    remote_debugging_port=debug_port,
                    debugger_host=debugger_host,
                    extension_dir=extension_dir,
                    browser_proxy=proxy_config.preview if proxy_config else "",
                    automation_result=automation_result,
                )
        except BrowserAutomationError:
            raise
        except Exception as exc:
            last_error = str(exc)
            continue

    detail = f": {last_error}" if last_error else ""
    raise BrowserLaunchError(f"没有找到可启动的 Chrome/Edge/Brave/Chromium 浏览器{detail}")


def cleanup_old_browser_profiles(profile_root: Path, *, max_age_hours: int = 72) -> None:
    root = Path(profile_root)
    if not root.exists():
        return
    cutoff = time.time() - max(1, int(max_age_hours or 72)) * 3600
    for child in root.iterdir():
        if not child.is_dir() or not child.name.startswith("checkout-"):
            continue
        try:
            if child.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(child)
        except Exception:
            continue


def execute_js_via_cdp(port: int, script: str, *, timeout_seconds: int = 15) -> tuple[str, Any]:
    js = str(script or "").strip()
    if not js:
        return "", None
    ws_url, host = _wait_for_page_websocket(port, timeout_seconds=timeout_seconds)
    return host, _run_coro_sync(_evaluate_js(ws_url, js, timeout_seconds=timeout_seconds))


def _validate_http_url(url: str) -> str:
    text = str(url or "").strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BrowserLaunchError("支付链接必须是 http/https URL")
    return text


def _new_profile_dir(profile_root: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = profile_root / f"checkout-{stamp}-{secrets.token_hex(5)}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _write_jp_profile_preferences(profile_dir: Path) -> None:
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs = {
        "autofill": {
            "credit_card_enabled": False,
            "profile_enabled": False,
        },
        "credentials_enable_service": False,
        "intl": {"accept_languages": "ja-JP,ja,en-US,en"},
        "payments": {"can_make_payment_enabled": False},
        "profile": {
            "password_manager_enabled": False,
        },
        "webkit": {"webprefs": {"default_encoding": "UTF-8"}},
    }
    (default_dir / "Preferences").write_text(json.dumps(prefs, ensure_ascii=False), encoding="utf-8")
    (profile_dir / "Local State").write_text(
        json.dumps(
            {
                "intl": {"app_locale": "ja-JP"},
                "browser": {"enabled_labs_experiments": []},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def _browser_proxy_config(proxy: object) -> _BrowserProxyConfig | None:
    raw = str(proxy or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return _BrowserProxyConfig(server_arg=raw, preview=_proxy_preview(raw))
    scheme = parsed.scheme.lower()
    if scheme == "socks5h":
        scheme = "socks5"
    if scheme not in {"http", "https", "socks4", "socks5"}:
        return _BrowserProxyConfig(server_arg=raw, preview=_proxy_preview(raw))
    host = parsed.hostname
    port = f":{parsed.port}" if parsed.port else ""
    server_arg = f"{scheme}://{host}{port}"
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    return _BrowserProxyConfig(
        server_arg=server_arg,
        preview=_proxy_preview(raw),
        username=username,
        password=password,
    )


def _proxy_preview(proxy: str) -> str:
    parsed = urlparse(str(proxy or "").strip())
    if parsed.scheme and parsed.hostname:
        port = f":{parsed.port}" if parsed.port else ""
        auth = "***@" if parsed.username or parsed.password else ""
        return f"{parsed.scheme}://{auth}{parsed.hostname}{port}"
    if "@" in proxy:
        return f"***@{proxy.rsplit('@', 1)[1]}"
    return proxy


def _normalize_checkout_sms_config(value: object) -> CheckoutSmsRuntimeConfig:
    if isinstance(value, CheckoutSmsRuntimeConfig):
        return value
    if value is None:
        return CheckoutSmsRuntimeConfig()
    if isinstance(value, dict):
        raw_numbers = value.get("numbers")
        timeout = value.get("timeoutSeconds", value.get("timeout_seconds", value.get("timeout", 180)))
        poll = value.get("pollSeconds", value.get("poll_seconds", value.get("poll", 2.0)))
        lease = value.get("lease") if isinstance(value.get("lease"), dict) else {}
        lease_acquire_url = str(
            lease.get("acquireUrl")
            or lease.get("acquire_url")
            or value.get("leaseAcquireUrl")
            or value.get("lease_acquire_url")
            or ""
        ).strip()
        lease_release_url = str(
            lease.get("releaseUrl")
            or lease.get("release_url")
            or value.get("leaseReleaseUrl")
            or value.get("lease_release_url")
            or ""
        ).strip()
        lease_token = str(
            lease.get("token")
            or value.get("leaseToken")
            or value.get("lease_token")
            or ""
        ).strip()
        lease_wait = (
            lease.get("waitSeconds")
            or lease.get("wait_seconds")
            or value.get("leaseWaitSeconds")
            or value.get("lease_wait_seconds")
            or 25.0
        )
    else:
        raw_numbers = value
        timeout = 180
        poll = 2.0
        lease_acquire_url = ""
        lease_release_url = ""
        lease_token = ""
        lease_wait = 25.0
    if isinstance(raw_numbers, dict):
        items = [raw_numbers]
    elif isinstance(raw_numbers, (list, tuple)):
        items = list(raw_numbers)
    else:
        items = []

    numbers: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if isinstance(item, dict):
            phone = str(item.get("phone") or item.get("number") or "").strip()
            sms_url = str(item.get("smsUrl") or item.get("sms_url") or item.get("url") or item.get("api") or "").strip()
            label = str(item.get("label") or item.get("name") or "").strip()
        else:
            phone = str(getattr(item, "phone", "") or "").strip()
            sms_url = str(
                getattr(item, "sms_url", "")
                or getattr(item, "smsUrl", "")
                or getattr(item, "url", "")
                or ""
            ).strip()
            label = str(getattr(item, "label", "") or getattr(item, "name", "") or "").strip()
        if not phone or not sms_url:
            continue
        key = (phone, sms_url)
        if key in seen:
            continue
        seen.add(key)
        numbers.append({"phone": phone, "smsUrl": sms_url, "label": label})
    return CheckoutSmsRuntimeConfig(
        numbers=tuple(numbers),
        timeout_seconds=_coerce_int(timeout, 180, minimum=5),
        poll_seconds=_coerce_float(poll, 2.0, minimum=1.0),
        lease_acquire_url=lease_acquire_url,
        lease_release_url=lease_release_url,
        lease_token=lease_token,
        lease_wait_seconds=_coerce_float(lease_wait, 25.0, minimum=1.0),
    )


def _coerce_int(value: object, default: int, *, minimum: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(minimum, parsed)


def _coerce_float(value: object, default: float, *, minimum: float) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    return max(minimum, parsed)


def _host_permission_for_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}/*"


def _build_auto_filler_extension(
    profile_dir: Path,
    script_path: Path,
    *,
    proxy_config: _BrowserProxyConfig | None = None,
    checkout_sms: CheckoutSmsRuntimeConfig | None = None,
) -> Path:
    source = Path(script_path)
    if not source.exists():
        raise BrowserLaunchError(f"自动填充脚本不存在: {source}")
    extension_dir = profile_dir / "auto_filler_extension"
    extension_dir.mkdir(parents=True, exist_ok=True)
    script = source.read_text(encoding="utf-8")
    checkout_sms = checkout_sms or CheckoutSmsRuntimeConfig()
    host_permissions = [
        *CHECKOUT_SCRIPT_MATCHES,
        *CHECKOUT_SCRIPT_CONNECTS,
        *[
            permission
            for permission in (_host_permission_for_url(item.get("smsUrl", "")) for item in checkout_sms.numbers)
            if permission
        ],
        *[
            permission
            for permission in (
                _host_permission_for_url(checkout_sms.lease_acquire_url),
                _host_permission_for_url(checkout_sms.lease_release_url),
            )
            if permission
        ],
    ]
    (extension_dir / "manifest.json").write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "name": "Protocol Reg Checkout Auto Filler",
                "version": "1.0.0",
                "description": "Inject checkout auto-filler script into PayPal/OpenAI/Stripe pages.",
                "permissions": ["scripting", "webRequest", "webRequestAuthProvider"],
                "host_permissions": list(dict.fromkeys(host_permissions)),
                "background": {"service_worker": "background.js"},
                "content_scripts": [
                    {
                        "matches": list(CHECKOUT_SCRIPT_MATCHES),
                        "js": ["env.js"],
                        "run_at": "document_start",
                    },
                    {
                        "matches": list(CHECKOUT_SCRIPT_MATCHES),
                        "js": ["content.js"],
                        "run_at": "document_idle",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (extension_dir / "background.js").write_text(_background_script(proxy_config), encoding="utf-8")
    (extension_dir / "env.js").write_text(_environment_content_script(), encoding="utf-8")
    (extension_dir / "content.js").write_text(_content_script(script, checkout_sms), encoding="utf-8")
    return extension_dir


def _background_script(proxy_config: _BrowserProxyConfig | None = None) -> str:
    username = json.dumps(proxy_config.username if proxy_config else "")
    password = json.dumps(proxy_config.password if proxy_config else "")
    auth_script = f"""
const PROXY_USERNAME = {username};
const PROXY_PASSWORD = {password};
if (PROXY_USERNAME || PROXY_PASSWORD) {{
  chrome.webRequest.onAuthRequired.addListener(
    () => ({{ authCredentials: {{ username: PROXY_USERNAME, password: PROXY_PASSWORD }} }}),
    {{ urls: ['<all_urls>'] }},
    ['blocking']
  );
}}
""".strip()
    return (auth_script + "\n\n" + r"""
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== 'GM_XMLHTTP_REQUEST') return false;
  const request = message.request || {};
  const headers = request.headers || {};
  fetch(request.url, {
    method: request.method || 'GET',
    headers,
    body: request.data,
    credentials: 'omit',
    cache: 'no-store'
  }).then(async (response) => {
    sendResponse({
      ok: true,
      status: response.status,
      statusText: response.statusText,
      responseText: await response.text()
    });
  }).catch((error) => {
    sendResponse({
      ok: false,
      status: 0,
      statusText: error && error.message ? error.message : 'request failed',
      responseText: ''
    });
  });
  return true;
});
""").strip()


def _environment_content_script() -> str:
    return r"""
(function() {
  const source = `(() => {
  try {
    const defineGetter = (target, prop, value) => {
      try {
        Object.defineProperty(target, prop, { get: () => value, configurable: true });
      } catch (_) {}
    };
    defineGetter(Navigator.prototype, 'language', 'ja-JP');
    defineGetter(Navigator.prototype, 'languages', ['ja-JP', 'ja', 'en-US', 'en']);
    defineGetter(navigator, 'language', 'ja-JP');
    defineGetter(navigator, 'languages', ['ja-JP', 'ja', 'en-US', 'en']);
    const RealDateTimeFormat = Intl.DateTimeFormat;
    function JPDateTimeFormat(locales, options) {
      options = Object.assign({}, options || {});
      if (!options.timeZone) options.timeZone = 'Asia/Tokyo';
      return new RealDateTimeFormat(locales || 'ja-JP', options);
    }
    JPDateTimeFormat.prototype = RealDateTimeFormat.prototype;
    Object.setPrototypeOf(JPDateTimeFormat, RealDateTimeFormat);
    JPDateTimeFormat.supportedLocalesOf = RealDateTimeFormat.supportedLocalesOf.bind(RealDateTimeFormat);
    Intl.DateTimeFormat = JPDateTimeFormat;
  } catch (error) {
    console.warn('[PP] 日本地区环境覆盖失败', error);
  }
})();`;
  const node = document.createElement('script');
  node.textContent = source;
  (document.documentElement || document.head || document.body).appendChild(node);
  node.remove();
})();
""".strip()


def _content_script(user_script: str, checkout_sms: CheckoutSmsRuntimeConfig) -> str:
    runtime_config = json.dumps(
        {"checkoutSms": checkout_sms.to_public_dict()},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"""
(function() {{
  window.__PROTOCOL_REG_CHECKOUT_CONFIG__ = Object.assign(
    {{}},
    window.__PROTOCOL_REG_CHECKOUT_CONFIG__ || {{}},
    {runtime_config}
  );

  function gmXmlHttpRequest(opts) {{
    opts = opts || {{}};
    chrome.runtime.sendMessage({{
      type: 'GM_XMLHTTP_REQUEST',
      request: {{
        method: opts.method || 'GET',
        url: opts.url,
        headers: opts.headers || {{}},
        data: opts.data
      }}
    }}, function(response) {{
      if (chrome.runtime.lastError) {{
        if (typeof opts.onerror === 'function') {{
          opts.onerror({{ status: 0, statusText: chrome.runtime.lastError.message || 'extension request failed', responseText: '' }});
        }}
        return;
      }}
      response = response || {{}};
      var payload = {{
        status: response.status || 0,
        statusText: response.statusText || '',
        responseText: response.responseText || ''
      }};
      if (response.ok) {{
        if (typeof opts.onload === 'function') opts.onload(payload);
      }} else if (typeof opts.onerror === 'function') {{
        opts.onerror(payload);
      }}
    }});
  }}
  window.GM_xmlhttpRequest = gmXmlHttpRequest;
  window.GM = window.GM || {{}};
  window.GM.xmlHttpRequest = gmXmlHttpRequest;
  try {{
{_indent_script(user_script, 4)}
  }} catch (error) {{
    console.error('[PP] 自动填充脚本执行失败', error);
  }}
}})();
""".strip()


def _indent_script(script: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line for line in script.splitlines())


def _browser_commands(
    profile_dir: Path,
    url: str,
    debug_port: int | None,
    *,
    extension_dir: Path | None = None,
    proxy_config: _BrowserProxyConfig | None = None,
) -> list[_BrowserCommand]:
    configured = str(os.environ.get("PROTOCOL_REG_BROWSER") or "").strip()
    if configured:
        return [_command_for_configured_browser(configured, profile_dir, url, debug_port, extension_dir=extension_dir, proxy_config=proxy_config)]
    if _is_wsl():
        return _wsl_browser_commands(profile_dir, url, debug_port, extension_dir=extension_dir, proxy_config=proxy_config)
    if sys.platform == "darwin":
        return _macos_browser_commands(profile_dir, url, debug_port, extension_dir=extension_dir, proxy_config=proxy_config)
    return _native_browser_commands(profile_dir, url, debug_port, extension_dir=extension_dir, proxy_config=proxy_config)


def _command_for_configured_browser(
    browser: str,
    profile_dir: Path,
    url: str,
    debug_port: int | None,
    *,
    extension_dir: Path | None = None,
    proxy_config: _BrowserProxyConfig | None = None,
) -> _BrowserCommand:
    if _is_wsl() and not shutil.which(browser):
        args = _browser_args(
            _windows_path(profile_dir),
            url,
            debug_port,
            extension_dir=_windows_path(extension_dir) if extension_dir else None,
            proxy_config=proxy_config,
        )
        return _BrowserCommand(
            name=browser,
            command=("cmd.exe", "/c", "start", "", browser, *args),
            wait_for_exit_code=True,
        )
    args = _browser_args(
        str(profile_dir),
        url,
        debug_port,
        extension_dir=str(extension_dir) if extension_dir else None,
        proxy_config=proxy_config,
    )
    return _BrowserCommand(name=Path(browser).name or browser, command=(browser, *args))


def _native_browser_commands(
    profile_dir: Path,
    url: str,
    debug_port: int | None,
    *,
    extension_dir: Path | None = None,
    proxy_config: _BrowserProxyConfig | None = None,
) -> list[_BrowserCommand]:
    names = [
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "brave-browser",
        "microsoft-edge",
        "msedge",
    ]
    args = _browser_args(
        str(profile_dir),
        url,
        debug_port,
        extension_dir=str(extension_dir) if extension_dir else None,
        proxy_config=proxy_config,
    )
    return [
        _BrowserCommand(name=name, command=(name, *args))
        for name in names
        if shutil.which(name)
    ]


def _wsl_browser_commands(
    profile_dir: Path,
    url: str,
    debug_port: int | None,
    *,
    extension_dir: Path | None = None,
    proxy_config: _BrowserProxyConfig | None = None,
) -> list[_BrowserCommand]:
    profile_arg = _windows_path(profile_dir)
    extension_arg = _windows_path(extension_dir) if extension_dir else None
    args = _browser_args(
        profile_arg,
        url,
        debug_port,
        remote_debugging_address="0.0.0.0",
        extension_dir=extension_arg,
        proxy_config=proxy_config,
    )
    commands: list[_BrowserCommand] = []
    for name in ("msedge", "chrome", "brave", "chromium"):
        commands.append(
            _BrowserCommand(
                name=name,
                command=("cmd.exe", "/c", "start", "", name, *args),
                wait_for_exit_code=True,
            )
        )
    return commands


def _macos_browser_commands(
    profile_dir: Path,
    url: str,
    debug_port: int | None,
    *,
    extension_dir: Path | None = None,
    proxy_config: _BrowserProxyConfig | None = None,
) -> list[_BrowserCommand]:
    args = _browser_args(
        str(profile_dir),
        url,
        debug_port,
        extension_dir=str(extension_dir) if extension_dir else None,
        proxy_config=proxy_config,
    )
    apps = [
        ("Google Chrome", "Google Chrome"),
        ("Microsoft Edge", "Microsoft Edge"),
        ("Brave Browser", "Brave Browser"),
        ("Chromium", "Chromium"),
    ]
    return [
        _BrowserCommand(
            name=name,
            command=("open", "-na", app, "--args", *args),
            wait_for_exit_code=True,
        )
        for name, app in apps
        if shutil.which("open")
    ]


def _browser_args(
    profile_dir: str,
    url: str,
    debug_port: int | None,
    *,
    remote_debugging_address: str = "127.0.0.1",
    extension_dir: str | None = None,
    proxy_config: _BrowserProxyConfig | None = None,
) -> list[str]:
    args = [
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--disable-save-password-bubble",
        "--lang=ja-JP",
        "--accept-lang=ja-JP,ja,en-US,en",
        "--timezone=Asia/Tokyo",
        "--new-window",
    ]
    if extension_dir:
        args.append(f"--load-extension={extension_dir}")
    if proxy_config:
        args.append(f"--proxy-server={proxy_config.server_arg}")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        args.append("--no-sandbox")
    if debug_port:
        args.extend(
            [
                f"--remote-debugging-port={debug_port}",
                f"--remote-debugging-address={remote_debugging_address}",
                "--remote-allow-origins=*",
            ]
        )
    args.append(url)
    return args


def _launch_browser(command: _BrowserCommand) -> bool:
    if command.command[0] != "cmd.exe" and shutil.which(command.command[0]) is None:
        return False
    stdout = subprocess.DEVNULL
    stderr = subprocess.DEVNULL
    if command.wait_for_exit_code:
        completed = subprocess.run(command.command, stdout=stdout, stderr=stderr, check=False, timeout=8)
        return completed.returncode == 0
    process = subprocess.Popen(command.command, stdout=stdout, stderr=stderr)
    time.sleep(0.35)
    return process.poll() is None or process.returncode == 0


def _windows_path(path: Path) -> str:
    try:
        completed = subprocess.run(
            ["wslpath", "-w", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
            timeout=5,
        )
        converted = completed.stdout.strip()
        if completed.returncode == 0 and converted:
            return converted
    except Exception:
        pass
    return str(path)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_page_websocket(port: int, *, timeout_seconds: int) -> tuple[str, str]:
    deadline = time.time() + max(1, timeout_seconds)
    hosts = _debugger_host_candidates()
    last_error = ""
    while time.time() < deadline:
        for host in hosts:
            try:
                targets = _fetch_json(f"http://{host}:{port}/json/list", timeout=1.0)
            except Exception as exc:
                last_error = str(exc)
                continue
            if not isinstance(targets, list):
                continue
            for target in targets:
                if not isinstance(target, dict):
                    continue
                if str(target.get("type") or "") != "page":
                    continue
                ws_url = str(target.get("webSocketDebuggerUrl") or "").strip()
                if ws_url:
                    return _rewrite_ws_host(ws_url, host, port), host
        time.sleep(0.25)
    detail = f": {last_error}" if last_error else ""
    raise BrowserAutomationError(f"无法连接浏览器 DevTools 调试端口{detail}")


def _debugger_host_candidates() -> list[str]:
    hosts: list[str] = []
    if _is_wsl():
        windows_host = _windows_host_ip()
        if windows_host:
            hosts.append(windows_host)
    hosts.extend(["127.0.0.1", "localhost"])
    deduped: list[str] = []
    for host in hosts:
        if host and host not in deduped:
            deduped.append(host)
    return deduped


def _windows_host_ip() -> str:
    try:
        for line in Path("/etc/resolv.conf").read_text(encoding="utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[0] == "nameserver":
                return parts[1]
    except Exception:
        return ""
    return ""


def _rewrite_ws_host(ws_url: str, host: str, port: int) -> str:
    parsed = urlparse(ws_url)
    if parsed.scheme not in {"ws", "wss"}:
        return ws_url
    return parsed._replace(netloc=f"{host}:{port}").geturl()


def _fetch_json(url: str, *, timeout: float) -> Any:
    try:
        with urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except URLError as exc:
        raise BrowserAutomationError(str(exc)) from exc


async def _evaluate_js(ws_url: str, script: str, *, timeout_seconds: int) -> Any:
    import websockets

    expression = f"(async () => {{\n{script}\n}})()"
    async with websockets.connect(ws_url, open_timeout=timeout_seconds, ping_interval=None) as websocket:
        await websocket.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
        await _recv_cdp_response(websocket, 1, timeout_seconds)
        await websocket.send(
            json.dumps(
                {
                    "id": 2,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": expression,
                        "awaitPromise": True,
                        "returnByValue": True,
                        "userGesture": True,
                    },
                }
            )
        )
        response = await _recv_cdp_response(websocket, 2, timeout_seconds)
    result = response.get("result") if isinstance(response, dict) else None
    if not isinstance(result, dict):
        raise BrowserAutomationError("JS 自动化没有返回有效结果")
    if result.get("exceptionDetails"):
        raise BrowserAutomationError(_cdp_exception_message(result["exceptionDetails"]))
    remote = result.get("result")
    if not isinstance(remote, dict):
        return None
    if remote.get("subtype") == "error":
        raise BrowserAutomationError(str(remote.get("description") or "JS 执行失败"))
    if "value" in remote:
        return remote.get("value")
    if "unserializableValue" in remote:
        return remote.get("unserializableValue")
    return remote.get("description")


async def _recv_cdp_response(websocket: Any, expected_id: int, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.time() + max(1, timeout_seconds)
    while time.time() < deadline:
        remaining = max(0.1, deadline - time.time())
        raw = await asyncio.wait_for(websocket.recv(), timeout=remaining)
        message = json.loads(raw)
        if isinstance(message, dict) and message.get("id") == expected_id:
            if message.get("error"):
                raise BrowserAutomationError(str(message["error"]))
            return message
    raise BrowserAutomationError("等待 JS 自动化结果超时")


def _cdp_exception_message(details: Any) -> str:
    if not isinstance(details, dict):
        return "JS 执行异常"
    exception = details.get("exception")
    if isinstance(exception, dict):
        description = str(exception.get("description") or "").strip()
        if description:
            return description
        value = str(exception.get("value") or "").strip()
        if value:
            return value
    text = str(details.get("text") or "").strip()
    return text or "JS 执行异常"


def _run_coro_sync(coro: Any) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def runner() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            error["exception"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error["exception"]
    return result.get("value")


def _is_wsl() -> bool:
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
    except Exception:
        return False
