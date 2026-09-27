"""Password hashing (scrypt), token digests and ids. Standard library only."""

import base64
import hashlib
import hmac
import secrets

from . import config

_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32)


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.scrypt(pw.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(dk).decode()


def check_password(pw: str, stored: str | None) -> bool:
    if not stored:
        hashlib.scrypt(b"-", salt=b"0" * 16, **_SCRYPT)  # same cost whether or not the account exists
        return False
    _, salt, dk = stored.split("$")
    got = hashlib.scrypt(pw.encode(), salt=base64.b64decode(salt), **_SCRYPT)
    return hmac.compare_digest(got, base64.b64decode(dk))


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def keyed(value: str) -> str:
    """HMAC for personal data we compare but never show (voter emails, IPs)."""
    return hmac.new(config.SECRET_KEY.encode(), value.strip().lower().encode(), "sha256").hexdigest()[:32]


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def new_token(n: int = 24) -> str:
    return secrets.token_urlsafe(n)
