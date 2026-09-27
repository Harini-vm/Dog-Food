"""Sliding-window rate limits, in memory.

Design choices (THREAT-MODEL.md has the reasoning):
  * Limits are per *person* first (voter, account, email address) and only loosely per network.
    A classroom or a conference Wi-Fi shares one address; a strict per-IP limit would lock out
    honest voters while barely slowing an attacker with many addresses.
  * An IPv4 address is one client. `::ffff:192.0.2.7` is the same client as `192.0.2.7`.
    IPv6 is grouped by /64, since one household or phone usually gets a whole /64.
  * One process holds the counters. The portal runs as a single app process (docker-compose.yml).
    Several processes would each count separately, which only makes limits looser, never stricter.
"""

import ipaddress
import threading
import time
from collections import defaultdict, deque

from fastapi import Request

from . import config
from .errors import HTTPError

_hits: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()

# name -> (max requests, window in seconds)
RULES = {
    "vote": (10, 60),            # per voter
    "vote_net": (600, 60),       # per network: loose, for shared Wi-Fi
    "signup_net": (100, 600),
    "login": (10, 600),          # per email address, failed attempts only
    "link_email": (5, 3600),     # voting links per address
    "link_net": (30, 600),
    "comment": (5, 60),          # per account
}


def client(request: Request) -> str:
    raw = request.client.host if request.client else "0.0.0.0"
    if config.TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            raw = fwd.split(",")[0].strip()
    return network(raw)


def network(raw: str) -> str:
    try:
        ip = ipaddress.ip_address(raw.strip().strip("[]"))
    except ValueError:
        return "unknown"
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.version == 6:
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


def hit(rule: str, key: str, now: float | None = None) -> None:
    """Count one request; raise 429 if the limit is already reached (the request is not counted)."""
    limit, window = RULES[rule]
    now = time.monotonic() if now is None else now
    with _lock:
        q = _hits[f"{rule}:{key}"]
        while q and q[0] < now - window:
            q.popleft()
        if len(q) >= limit:
            retry = int(q[0] + window - now) + 1
            raise HTTPError(429, "rate_limited", f"too many requests; try again in {retry} s")
        q.append(now)


def check(rule: str, key: str, now: float | None = None) -> None:
    """Raise 429 if the limit is reached, without counting this request. Login uses it so that
    after 10 failures even the right password is refused until the window passes."""
    limit, window = RULES[rule]
    now = time.monotonic() if now is None else now
    with _lock:
        q = _hits.get(f"{rule}:{key}")
        if q and len([t for t in q if t >= now - window]) >= limit:
            raise HTTPError(429, "rate_limited", "too many failed attempts; try again later")


def reset() -> None:
    with _lock:
        _hits.clear()
