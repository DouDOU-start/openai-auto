from __future__ import annotations

import random
import secrets
import string
from datetime import datetime


FIRST_NAMES = ["James", "John", "Robert", "Michael", "William", "Emma", "Olivia", "Ava", "Sophia", "Mia"]
LAST_NAMES = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Wilson"]


def mask_email(email: str) -> str:
    if "@" not in email:
        return email
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        return f"{name[:1]}***@{domain}"
    return f"{name[:2]}***@{domain}"


def make_password(length: int = 20) -> str:
    required = [
        random.choice(string.ascii_uppercase),
        random.choice(string.ascii_lowercase),
        random.choice(string.digits),
        random.choice("!@#$%&*"),
    ]
    pool = string.ascii_letters + string.digits + "!@#$%&*"
    rest = [random.choice(pool) for _ in range(max(0, length - len(required)))]
    chars = required + rest
    random.shuffle(chars)
    return "".join(chars)


def make_random_email(suffixes: tuple[str, ...], used_emails: set[str] | None = None) -> str:
    """生成更像正常用户的随机邮箱，并避开传入的已占用邮箱。"""

    if not suffixes:
        raise ValueError("随机邮箱需要先在配置文件设置 email_suffixes")
    used = {item.strip().lower() for item in (used_emails or set()) if item.strip()}
    for _ in range(1000):
        suffix = secrets.choice(suffixes)
        for local in _make_email_local_parts():
            email = f"{local}@{suffix}".lower()
            if email not in used:
                return email
    raise ValueError("随机邮箱生成失败：配置后缀下的候选邮箱均与已有记录冲突")


def _make_email_local_parts() -> tuple[str, ...]:
    first = _email_token(secrets.choice(FIRST_NAMES))
    last = _email_token(secrets.choice(LAST_NAMES))
    suffix = f"{secrets.randbelow(100):02d}"
    return (
        f"{first}.{last}",
        f"{first}{last}",
        f"{first[0]}{last}",
        f"{first}.{last}{suffix}",
        f"{first}{last}{suffix}",
        f"{first[0]}{last}{suffix}",
    )


def _email_token(value: str) -> str:
    token = "".join(ch for ch in value.lower() if ch.isalnum())
    return token or "user"


def random_profile() -> dict[str, str]:
    year = random.randint(datetime.now().year - 45, datetime.now().year - 18)
    month = random.randint(1, 12)
    day = random.randint(1, 28)
    return {
        "name": f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
        "birthdate": f"{year:04d}-{month:02d}-{day:02d}",
    }


def absolutize_auth_url(raw_url: str) -> str:
    if not raw_url:
        return ""
    if raw_url.startswith(("http://", "https://")):
        return raw_url
    return f"https://auth.openai.com{raw_url}"
