from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .settings import parse_proxy_list


@dataclass(frozen=True)
class AirGateMonitorConfig:
    enabled: bool = False
    core_url: str = ""
    admin_key: str = ""
    proxy: str = ""
    poll_interval_seconds: int = 300
    page_size: int = 100
    relogin_concurrency: int = 3


@dataclass(frozen=True)
class CheckoutSmsNumberConfig:
    phone: str
    sms_url: str
    label: str = ""


@dataclass(frozen=True)
class AppConfig:
    # 协议请求代理
    proxy: str = ""
    # 多代理列表；出网调用会按列表顺序轮询使用
    proxies: tuple[str, ...] = ()
    # Web 端任务最大并发数
    max_concurrency: int = 3
    # 注册邮箱随机生成后缀
    email_suffixes: tuple[str, ...] = ()
    # Cloudflare-email 验证码 API
    email_code_api: str = ""
    email_code_key: str = ""
    email_code_sender_suffix: str = "openai.com"
    email_code_timeout: int = 120
    email_code_poll: float = 2.0
    # 邮箱验证码失败后的重试轮次
    otp_max_retries: int = 5
    # 每轮等待验证码时的最大轮询次数
    otp_poll_max_attempts: int = 20
    # 邮箱验证码 API 是否也走代理
    use_proxy_for_email: bool = False
    # SMSBower 接码 API
    smsbower_api: str = "https://smsbower.page/stubs/handler_api.php"
    smsbower_key: str = ""
    smsbower_service: str = "dr"
    smsbower_country: str = ""
    smsbower_max_price: str = ""
    smsbower_min_price: str = ""
    smsbower_provider_ids: str = ""
    smsbower_except_provider_ids: str = ""
    smsbower_phone_exception: str = ""
    smsbower_timeout: int = 30
    smsbower_poll: float = 5.0
    use_proxy_for_smsbower: bool = True
    smsbower_reuse_limit: int = 3
    # Checkout 浏览器自动填手机号和短信验证码
    checkout_sms_numbers: tuple[CheckoutSmsNumberConfig, ...] = ()
    checkout_sms_timeout: int = 180
    checkout_sms_poll: float = 2.0
    # AirGate core 401 账号自动修复
    airgate_monitor: AirGateMonitorConfig = AirGateMonitorConfig()


def load_app_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return AppConfig()

    proxy = str(raw.get("proxy") or "").strip()
    proxies = parse_proxy_list(raw.get("proxies")) or parse_proxy_list(proxy)
    max_concurrency = _positive_int(raw.get("max_concurrency"), 3)
    email_suffixes = _load_email_suffixes(raw.get("email_suffixes"))

    mail = raw.get("email_code")
    if not isinstance(mail, dict):
        mail = {}

    def _s(key: str, default: str = "") -> str:
        v = mail.get(key)
        return str(v).strip() if v is not None else default

    def _i(key: str, default: int) -> int:
        v = mail.get(key)
        try:
            return int(v)
        except Exception:
            return default

    def _f(key: str, default: float) -> float:
        v = mail.get(key)
        try:
            return float(v)
        except Exception:
            return default

    sender = _s("sender_suffix", "openai.com") or "openai.com"
    poll = _f("poll", 2.0)
    if poll < 0.5:
        poll = 0.5
    timeout = _i("timeout", 120)
    if timeout < 5:
        timeout = 5
    otp_max_retries = _positive_int(mail.get("max_otp_retries"), 5)
    otp_poll_max_attempts = _positive_int(mail.get("otp_poll_max_attempts"), 20)
    use_proxy_value = mail.get("use_proxy")
    if use_proxy_value in {None, ""} and "use_proxy_for_email" in mail:
        use_proxy_value = mail.get("use_proxy_for_email")
    use_proxy_for_email = _bool(use_proxy_value, False)
    smsbower = raw.get("smsbower")
    if not isinstance(smsbower, dict):
        smsbower = {}

    def _sms_s(key: str, default: str = "") -> str:
        v = smsbower.get(key)
        return str(v).strip() if v is not None else default

    def _sms_i(key: str, default: int) -> int:
        v = smsbower.get(key)
        try:
            return int(v)
        except Exception:
            return default

    def _sms_f(key: str, default: float) -> float:
        v = smsbower.get(key)
        try:
            return float(v)
        except Exception:
            return default

    smsbower_timeout = _sms_i("timeout", 30)
    if smsbower_timeout < 5:
        smsbower_timeout = 5
    smsbower_poll = _sms_f("poll", 5.0)
    if smsbower_poll < 1:
        smsbower_poll = 1.0
    smsbower_reuse_limit = _sms_i("reuse_limit", 3)
    if smsbower_reuse_limit < 1:
        smsbower_reuse_limit = 1
    smsbower_use_proxy_value = smsbower.get("use_proxy")
    if smsbower_use_proxy_value in {None, ""} and "use_proxy_for_smsbower" in smsbower:
        smsbower_use_proxy_value = smsbower.get("use_proxy_for_smsbower")
    checkout_sms = raw.get("checkout_sms")
    if not isinstance(checkout_sms, dict):
        checkout_sms = {}
    checkout_sms_timeout = _positive_int(checkout_sms.get("timeout"), 180)
    if checkout_sms_timeout < 5:
        checkout_sms_timeout = 5
    checkout_sms_poll = _float(checkout_sms.get("poll"), 2.0)
    if checkout_sms_poll < 1:
        checkout_sms_poll = 1.0
    airgate_monitor = _load_airgate_monitor(raw.get("airgate_monitor"))

    return AppConfig(
        proxy=proxy,
        proxies=proxies,
        max_concurrency=max_concurrency,
        email_suffixes=email_suffixes,
        email_code_api=_s("api", ""),
        email_code_key=_s("key", ""),
        email_code_sender_suffix=sender,
        email_code_timeout=timeout,
        email_code_poll=poll,
        otp_max_retries=otp_max_retries,
        otp_poll_max_attempts=otp_poll_max_attempts,
        use_proxy_for_email=use_proxy_for_email,
        smsbower_api=_sms_s("api", "https://smsbower.page/stubs/handler_api.php"),
        smsbower_key=_sms_s("key", ""),
        smsbower_service=_sms_s("service", "dr") or "dr",
        smsbower_country=_sms_s("country", ""),
        smsbower_max_price=_sms_s("max_price", ""),
        smsbower_min_price=_sms_s("min_price", ""),
        smsbower_provider_ids=_sms_s("provider_ids", ""),
        smsbower_except_provider_ids=_sms_s("except_provider_ids", ""),
        smsbower_phone_exception=_sms_s("phone_exception", ""),
        smsbower_timeout=smsbower_timeout,
        smsbower_poll=smsbower_poll,
        use_proxy_for_smsbower=_bool(smsbower_use_proxy_value, True),
        smsbower_reuse_limit=smsbower_reuse_limit,
        checkout_sms_numbers=_load_checkout_sms_numbers(checkout_sms.get("numbers")),
        checkout_sms_timeout=checkout_sms_timeout,
        checkout_sms_poll=checkout_sms_poll,
        airgate_monitor=airgate_monitor,
    )


def save_airgate_monitor_config(path: Path, config: AirGateMonitorConfig) -> None:
    raw = _load_raw_config(path)
    raw["airgate_monitor"] = {
        "enabled": bool(config.enabled),
        "core_url": str(config.core_url or "").strip(),
        "admin_key": str(config.admin_key or "").strip(),
        "proxy": str(config.proxy or "").strip(),
        "poll_interval_seconds": max(10, int(config.poll_interval_seconds or 300)),
        "page_size": min(100, max(1, int(config.page_size or 100))),
        "relogin_concurrency": min(10, max(1, int(config.relogin_concurrency or 3))),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def save_checkout_sms_config(
    path: Path,
    numbers: tuple[CheckoutSmsNumberConfig, ...],
    *,
    timeout: int = 180,
    poll: float = 2.0,
) -> None:
    raw = _load_raw_config(path)
    raw["checkout_sms"] = {
        "timeout": max(5, int(timeout or 180)),
        "poll": max(1.0, float(poll or 2.0)),
        "numbers": [
            {
                "phone": item.phone,
                "sms_url": item.sms_url,
                "label": item.label,
            }
            for item in numbers
            if item.phone and item.sms_url
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _load_raw_config(path: Path) -> dict[str, Any]:
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    return {}


def config_template() -> dict[str, Any]:
    return {
        "proxy": "",
        "proxies": [],
        "max_concurrency": 3,
        "email_suffixes": [],
        "email_code": {
            "api": "",
            "key": "",
            "sender_suffix": "openai.com",
            "timeout": 120,
            "poll": 2.0,
            "max_otp_retries": 5,
            "otp_poll_max_attempts": 20,
            "use_proxy": False,
        },
        "smsbower": {
            "api": "https://smsbower.page/stubs/handler_api.php",
            "key": "",
            "service": "dr",
            "country": "",
            "max_price": "",
            "min_price": "",
            "provider_ids": "",
            "except_provider_ids": "",
            "phone_exception": "",
            "timeout": 30,
            "poll": 5.0,
            "use_proxy": True,
            "reuse_limit": 3,
        },
        "checkout_sms": {
            "timeout": 180,
            "poll": 2.0,
            "numbers": [
                {
                    "phone": "",
                    "sms_url": "",
                    "label": "",
                }
            ],
        },
        "airgate_monitor": {
            "enabled": False,
            "core_url": "",
            "admin_key": "",
            "proxy": "",
            "poll_interval_seconds": 300,
            "page_size": 100,
            "relogin_concurrency": 3,
        },
    }


def _load_checkout_sms_numbers(value: object) -> tuple[CheckoutSmsNumberConfig, ...]:
    if isinstance(value, dict):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return ()

    numbers: list[CheckoutSmsNumberConfig] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if isinstance(item, dict):
            phone = str(item.get("phone") or item.get("number") or "").strip()
            sms_url = str(item.get("sms_url") or item.get("url") or item.get("api") or "").strip()
            label = str(item.get("label") or item.get("name") or "").strip()
        else:
            phone = ""
            sms_url = str(item or "").strip()
            label = ""
        if not phone or not sms_url:
            continue
        key = (phone, sms_url)
        if key in seen:
            continue
        seen.add(key)
        numbers.append(CheckoutSmsNumberConfig(phone=phone, sms_url=sms_url, label=label))
    return tuple(numbers)


def _load_airgate_monitor(value: object) -> AirGateMonitorConfig:
    if not isinstance(value, dict):
        return AirGateMonitorConfig()

    def _s(key: str, default: str = "") -> str:
        v = value.get(key)
        return str(v).strip() if v is not None else default

    poll = _positive_int(value.get("poll_interval_seconds"), 300)
    if poll < 10:
        poll = 10
    page_size = _positive_int(value.get("page_size"), 100)
    page_size = min(100, max(1, page_size))
    relogin_concurrency = _positive_int(value.get("relogin_concurrency"), 3)
    relogin_concurrency = min(10, max(1, relogin_concurrency))
    return AirGateMonitorConfig(
        enabled=_bool(value.get("enabled"), False),
        core_url=_s("core_url"),
        admin_key=_s("admin_key"),
        proxy=_s("proxy"),
        poll_interval_seconds=poll,
        page_size=page_size,
        relogin_concurrency=relogin_concurrency,
    )


def _load_email_suffixes(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, list):
        items = value
    else:
        return ()

    suffixes: list[str] = []
    seen: set[str] = set()
    for item in items:
        suffix = str(item or "").strip().lower()
        if suffix.startswith("@"):
            suffix = suffix[1:]
        if not suffix or "@" in suffix or "." not in suffix:
            continue
        if suffix in seen:
            continue
        seen.add(suffix)
        suffixes.append(suffix)
    return tuple(suffixes)


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


def _float(value: object, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default
