#!/usr/bin/env python3
"""Verify a DOGFOOD certificate offline.

    python tools/verify_record.py record.json keys.json

record.json: the response of GET /api/v1/certificates/<id>  ({"record": ..., "signature": ...})
keys.json:   the response of GET /.well-known/dogfood-keys.json
Needs only the `cryptography` package. The server does not have to exist any more.
"""

import base64
import json
import sys

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def main(record_path: str, keys_path: str) -> int:
    doc = json.load(open(record_path, encoding="utf-8"))
    keys = json.load(open(keys_path, encoding="utf-8"))
    record, signature = doc["record"], doc["signature"]
    key = next((k for k in keys["keys"] if k["kid"] == record.get("key_id")), None)
    if key is None:
        print("INVALID: no public key with id", record.get("key_id"))
        return 1
    signed = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key["public_key"])).verify(base64.b64decode(signature), signed)
    except Exception:
        print("INVALID: the signature does not match this record")
        return 1
    print(f"VALID: {record['kind']} certificate for {record['recipient']}, {record['event']['name']}, "
          f"issued {record['issued_at']}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(main(sys.argv[1], sys.argv[2]))
