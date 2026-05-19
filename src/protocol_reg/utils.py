from __future__ import annotations

import random
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
