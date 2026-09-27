"""Signed records: certificates anyone can verify offline.

A record is a small JSON object. We sign the *canonical* form (sorted keys, no spaces, UTF-8) with
Ed25519. Re-formatting the JSON therefore does not break verification, but changing any value does.
Anyone can check a record with the public key from /.well-known/dogfood-keys.json and
tools/verify_record.py, even if this server is gone.

Each record carries the SHA-256 of the published results it came from. If the results were ever
retracted and republished differently, the record still verifies (it was genuinely issued) but the
verify endpoint reports that it no longer matches the current results. Retracting after issuing is
refused anyway, unless an admin deliberately forces it.
"""

import base64
import hashlib
import json
import os
from datetime import datetime, timezone

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .. import audit, config, db
from ..errors import bad, conflict
from ..security import new_id
from . import results

_key = None


def canonical(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _load():
    """The signing key lives in DOGFOOD_DATA_DIR (a Docker volume), created on first use, mode 600."""
    global _key
    if _key is None:
        path = config.DATA_DIR / "signing-key.pem"
        if path.exists():
            _key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        else:
            config.DATA_DIR.mkdir(parents=True, exist_ok=True)
            _key = Ed25519PrivateKey.generate()
            pem = _key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption())
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(pem)
    return _key


def public_raw() -> bytes:
    return _load().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def key_id() -> str:
    return hashlib.sha256(public_raw()).hexdigest()[:16]


def public_keys() -> dict:
    return {"keys": [{"kid": key_id(), "alg": "Ed25519",
                      "public_key": base64.b64encode(public_raw()).decode()}]}


def check(record: dict, signature: str, keys: dict) -> bool:
    """Offline-capable verification: needs only the record, the signature and the public keys."""
    k = next((k for k in keys.get("keys", []) if k["kid"] == record.get("key_id")), None)
    if k is None:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(k["public_key"])).verify(
            base64.b64decode(signature), canonical(record).encode())
        return True
    except Exception:
        return False


def issue(conn, user, ev) -> dict:
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s FOR UPDATE", (ev["id"],))
    snap = results.published(conn, ev["id"])
    if snap is None or not ev["results_published_at"]:
        raise conflict("publish results first", "not_published")
    if db.one(conn, "SELECT 1 FROM certificates WHERE event_id = %s AND record->>'results_sha256' = %s",
              (ev["id"], snap["body_hash"])):
        raise conflict("certificates for these results are already issued", "already_issued")
    body = snap["body"]
    issued_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base = {"event": {"id": ev["id"], "name": ev["name"]}, "results_version": snap["version"],
            "results_sha256": snap["body_hash"], "issued_at": issued_at, "key_id": key_id(),
            "issuer": config.BASE_URL}
    made = []

    def members(pid):
        return db.rows(conn, """SELECT u.name FROM projects p JOIN team_members m ON m.team_id = p.team_id
                                JOIN users u ON u.id = m.user_id WHERE p.id = %s ORDER BY u.name""", (pid,))

    def add(kind, r, **extra):
        for m in members(r["project_id"]):
            cid = new_id("crt")
            rec = {**base, "id": cid, "kind": kind, "recipient": m["name"], "team": r["team"],
                   "project": {"id": r["project_id"], "title": r["title"]}, **extra}
            text = canonical(rec)
            sig = base64.b64encode(_load().sign(text.encode())).decode()
            conn.execute("""INSERT INTO certificates (id, event_id, kind, record, canonical, signature, key_id)
                            VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                         (cid, ev["id"], kind, db.jsonb(rec), text, sig, rec["key_id"]))
            made.append(cid)

    for r in body["ranking"]:
        if r["rank"] <= 3:
            add("winner", r, place=r["rank"])
        if r["track"] and r["track_rank"] == 1:
            add("track_winner", r, track=r["track"])
        add("participant", r)
    audit.log(conn, user, "certificates.issue", ev["id"], ev["id"], {"count": len(made), "results": snap["body_hash"]})
    from . import webhooks
    webhooks.emit(conn, ev["id"], "certificates.issued", {"count": len(made), "results_version": snap["version"]})
    return {"issued": len(made)}


def get(conn, cid: str):
    return db.one(conn, "SELECT * FROM certificates WHERE id = %s", (cid,))


def verify(conn, record: dict, signature: str) -> dict:
    if not isinstance(record, dict) or not isinstance(signature, str):
        raise bad("send {record, signature}")
    ok = check(record, signature, public_keys())
    out = {"valid": ok}
    if ok:
        snap = results.published(conn, (record.get("event") or {}).get("id", ""))
        out["matches_current_results"] = bool(snap and snap["body_hash"] == record.get("results_sha256"))
    return out
