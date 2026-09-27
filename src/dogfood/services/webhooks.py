"""Outgoing webhooks: an outbox table, a background sender, HMAC signatures, retries.

Envelope (the JSON body):
  {"id": "whe_...", "sequence": 42, "type": "results.published", "occurred_at": "...", "event_id": "...",
   "data": {...}}
Headers:
  X-Dogfood-Event-Id: the envelope id (same on every retry: deduplicate on it)
  X-Dogfood-Signature: t=<unix time>,v1=<hex HMAC-SHA256(secret, "<t>.<raw body>")>
Verify the signature over the raw bytes *before* parsing JSON; reject old timestamps to stop replays.
Retries: 6 attempts, waiting 5 s, 25 s, 2 min, 10 min, 50 min. Then the delivery is marked failed and
shown to organizers.

Receivers on private or loopback addresses are refused unless DOGFOOD_WEBHOOK_ALLOW_PRIVATE is set,
so an organizer account cannot be used to make the server call the database or a cloud metadata
service (SSRF).
"""

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

from .. import audit, config, db
from ..errors import bad, not_found
from ..security import new_id, new_token

log = logging.getLogger("dogfood.webhooks")
MAX_ATTEMPTS = 6
BACKOFF = [5, 25, 120, 600, 3000]
TYPES = ("project.submitted", "results.published", "results.retracted", "certificates.issued", "ping")


NAT64 = ipaddress.ip_network("64:ff9b::/96")
IPV4_COMPAT = ipaddress.ip_network("::/96")              # deprecated IPv4-compatible form, e.g. ::127.0.0.1


def _public(ip) -> bool:
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.version == 6 and (ip in NAT64 or ip in IPV4_COMPAT):   # 64:ff9b::a9fe:a9fe = 169.254.169.254 via NAT64
        ip = ipaddress.ip_address(int(ip) & 0xFFFFFFFF)
    return ip.is_global


def resolve(url: str) -> tuple[str, str, int, str]:
    """Validate the URL and return (scheme, host, port, ip): one checked address to connect to."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise bad("webhook URL must be http:// or https:// with a host")
    if parts.username or parts.password:
        raise bad("put credentials in the receiver's own check of our signature, not in the URL")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        addrs = [ai[4][0].split("%")[0] for ai in socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)]
    except OSError:
        raise bad("cannot resolve that host")
    if not addrs:
        raise bad("cannot resolve that host")
    if not config.WEBHOOK_ALLOW_PRIVATE and not all(_public(ipaddress.ip_address(a)) for a in addrs):
        raise bad("webhook receivers must be public addresses", "private_address")
    return parts.scheme, parts.hostname, port, addrs[0]


def check_url(url: str) -> str:
    resolve(url)
    return url.strip()


def add(conn, user, ev, url: str) -> dict:
    wid, secret = new_id("whk"), "whsec_" + new_token(24)
    conn.execute("INSERT INTO webhooks (id, event_id, url, secret, created_by) VALUES (%s, %s, %s, %s, %s)",
                 (wid, ev["id"], check_url(url), secret, user.id))
    audit.log(conn, user, "webhook.add", ev["id"], wid, {"url": url})
    return {"id": wid, "secret": secret}


def remove(conn, user, ev, wid: str) -> None:
    if not conn.execute("DELETE FROM webhooks WHERE id = %s AND event_id = %s", (wid, ev["id"])).rowcount:
        raise not_found("no such webhook")
    audit.log(conn, user, "webhook.remove", ev["id"], wid)


def emit(conn, event_id: str, type_: str, data: dict) -> None:
    """Call inside the transaction that makes the change."""
    assert type_ in TYPES
    eid = new_id("whe")
    seq = db.val(conn, "SELECT nextval('webhook_events_sequence_seq')")
    at = db.val(conn, "SELECT to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS\"Z\"')")
    body = json.dumps({"id": eid, "sequence": seq, "type": type_, "occurred_at": at, "event_id": event_id,
                       "data": data}, separators=(",", ":"), sort_keys=True, default=str)
    conn.execute("INSERT INTO webhook_events (sequence, id, event_id, type, body) VALUES (%s, %s, %s, %s, %s)",
                 (seq, eid, event_id, type_, body))
    conn.execute("""INSERT INTO webhook_deliveries (webhook_id, sequence)
                    SELECT id, %s FROM webhooks WHERE event_id = %s AND active""", (seq, event_id))


def sign(secret: str, body: bytes, ts: int) -> str:
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify(secret: str, body: bytes, header: str, tolerance: int = 300, now: int | None = None) -> bool:
    """What a receiver does. Included so tests (and the docs) use the exact same rule."""
    try:
        parts = dict(p.split("=", 1) for p in header.split(","))
        ts = int(parts["t"])
    except (ValueError, KeyError):
        return False
    if abs((now or int(time.time())) - ts) > tolerance:
        return False
    return hmac.compare_digest(sign(secret, body, ts), f"t={ts},v1={parts.get('v1', '')}")


def _send(url: str, secret: str, event_id: str, body: str) -> tuple[int | None, str | None]:
    """POST to the receiver. The host is resolved and checked once, and the connection goes to exactly
    that address (DNS rebinding cannot swap in an internal one). TLS still verifies the real host name.
    Redirects are never followed: http.client does not follow them."""
    try:
        scheme, host, port, ip = resolve(url)
    except Exception as e:
        return None, "blocked: " + getattr(e, "message", str(e))
    parts = urlsplit(url.strip())
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    raw = body.encode()
    host_header = f"[{host}]" if ":" in host else host
    if parts.port:
        host_header += f":{parts.port}"
    headers = {"Content-Type": "application/json", "User-Agent": "dogfood-webhooks/1", "Host": host_header,
               "X-Dogfood-Event-Id": event_id, "X-Dogfood-Signature": sign(secret, raw, int(time.time()))}
    timeout = config.WEBHOOK_TIMEOUT
    try:
        if scheme == "https":
            ctx = ssl.create_default_context()

            class Pinned(http.client.HTTPSConnection):
                def connect(self):
                    sock = socket.create_connection((ip, port), timeout=timeout)
                    self.sock = ctx.wrap_socket(sock, server_hostname=host)
            conn = Pinned(host, port, timeout=timeout, context=ctx)
        else:
            class PinnedHTTP(http.client.HTTPConnection):
                def connect(self):
                    self.sock = socket.create_connection((ip, port), timeout=timeout)
            conn = PinnedHTTP(host, port, timeout=timeout)
        try:
            conn.request("POST", path, body=raw, headers=headers)
            status = conn.getresponse().status
        finally:
            conn.close()
    except Exception as e:                                          # timeouts, refused connections, TLS errors
        return None, type(e).__name__ + ": " + str(e)[:200]
    if 300 <= status < 400:
        return status, f"HTTP {status}: redirect not followed (receivers must answer directly)"
    return status, None if 200 <= status < 300 else f"HTTP {status}"


def deliver_one() -> bool:
    """Send one due delivery. Each delivery is its own short transaction, so a slow receiver only
    holds its own row. SKIP LOCKED lets several workers share the queue without double sends."""
    with db.tx() as conn:
        d = db.one(conn, """SELECT d.webhook_id, d.sequence, d.attempts, w.url, w.secret, e.id AS eid, e.body
                            FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id
                            JOIN webhook_events e ON e.sequence = d.sequence
                            WHERE d.status = 'pending' AND d.next_at <= now()
                            ORDER BY d.sequence LIMIT 1 FOR UPDATE OF d SKIP LOCKED""")
        if d is None:
            return False
        status, err = _send(d["url"], d["secret"], d["eid"], d["body"])
        n = d["attempts"] + 1
        if status is not None and 200 <= status < 300:
            conn.execute("""UPDATE webhook_deliveries SET status = 'delivered', attempts = %s, last_status = %s,
                              last_error = NULL, delivered_at = now() WHERE webhook_id = %s AND sequence = %s""",
                         (n, status, d["webhook_id"], d["sequence"]))
        else:
            wait = BACKOFF[min(n - 1, len(BACKOFF) - 1)] * config.WEBHOOK_BACKOFF_SCALE
            conn.execute("""UPDATE webhook_deliveries SET attempts = %s, last_status = %s, last_error = %s,
                              status = CASE WHEN %s >= %s THEN 'failed' ELSE 'pending' END,
                              next_at = now() + make_interval(secs => %s)
                            WHERE webhook_id = %s AND sequence = %s""",
                         (n, status, err, n, MAX_ATTEMPTS, wait, d["webhook_id"], d["sequence"]))
    return True


def deliver_due(limit: int = 50) -> int:
    n = 0
    while n < limit and deliver_one():
        n += 1
    return n


class Worker(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True, name="webhooks")
        self.stop = threading.Event()

    def run(self):
        while not self.stop.is_set():
            try:
                if deliver_due() == 0:
                    self.stop.wait(1.0)
            except Exception:                                      # never let the sender die
                log.exception("webhook delivery loop")
                self.stop.wait(5.0)
