"""Hash-chained audit log, written in the same transaction as the change it describes."""

import hashlib
import json

from . import db

GENESIS = "0" * 64


def _hash(prev, at, actor, action, event_id, subject, detail) -> str:
    body = json.dumps([prev, at, actor, action, event_id, subject, detail], sort_keys=True, default=str)
    return hashlib.sha256(body.encode()).hexdigest()


def log(conn, actor, action: str, event_id=None, subject: str = "", detail: dict | None = None) -> None:
    detail = detail or {}
    conn.execute("SELECT pg_advisory_xact_lock(4202)")          # one writer at a time keeps the chain linear
    prev = db.val(conn, "SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1") or GENESIS
    at = db.val(conn, "SELECT to_char(clock_timestamp() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS.US')")
    who = getattr(actor, "id", actor)
    h = _hash(prev, at, who, action, event_id, subject, detail)
    conn.execute("""INSERT INTO audit_log (at, actor, action, event_id, subject, detail, prev_hash, hash)
                    VALUES (%s::timestamp AT TIME ZONE 'UTC', %s, %s, %s, %s, %s, %s, %s)""",
                 (at, who, action, event_id, subject, db.jsonb(detail), prev, h))


def verify(conn) -> tuple[bool, int, int | None]:
    prev, n = GENESIS, 0
    for r in conn.execute("""SELECT id, to_char(at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US') AS at, actor,
                                    action, event_id, subject, detail, prev_hash, hash FROM audit_log ORDER BY id"""):
        n += 1
        if r["prev_hash"] != prev or r["hash"] != _hash(prev, r["at"], r["actor"], r["action"], r["event_id"],
                                                         r["subject"], r["detail"]):
            return False, n, r["id"]
        prev = r["hash"]
    return True, n, None
