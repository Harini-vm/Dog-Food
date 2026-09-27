"""First boot: import fixtures.json and, in demo mode, create fixed credentials for the checker.

Demo mode is for evaluation only. It gives every seeded account the password "dogfood" and creates
the four bearer tokens .dogfood.toml refers to. With DOGFOOD_DEMO=false they are revoked at boot.
"""

import json
import logging

from . import config, db
from .security import digest, hash_password, new_id
from .services import importer

log = logging.getLogger("dogfood.seed")
PASSWORD = "dogfood"
TOKENS = {  # role -> (account, token)
    "admin": ("admin@dogfood.local", "df_admin_demo"),
    "organizer": ("organizer@dogfood.local", "df_organizer_demo"),
    "judge_a": ("diego.herrera@example.org", "df_judge_a_demo"),      # fixture judge jdg_24
    "judge_b": ("jonas.vogel@example.org", "df_judge_b_demo"),        # fixture judge jdg_26
    "participant": ("priya1@example.org", "df_participant_demo"),     # captain of team NorthKiln
}


def run() -> None:
    pw = hash_password(PASSWORD) if config.DEMO else None
    with db.tx() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(4203)")
        for email, name, admin in (("admin@dogfood.local", "Ada Admin", True),
                                   ("organizer@dogfood.local", "Olu Organizer", False)):
            conn.execute("""INSERT INTO users (id, email, name, password_hash, is_admin) VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT (email) DO NOTHING""", (new_id("usr"), email, name, pw, admin))
        if config.SEED and not db.val(conn, "SELECT count(*) FROM events") and config.FIXTURES.exists():
            org = db.val(conn, "SELECT id FROM users WHERE email = 'organizer@dogfood.local'")
            report = importer.import_event(conn, json.loads(config.FIXTURES.read_text()), organizers=[org], pw_hash=pw)
            log.info("seeded %s: %s", report["event_id"], {k: report[k] for k in ("projects", "teams", "judges")})
        if not config.DEMO:
            conn.execute("UPDATE api_tokens SET revoked_at = now() WHERE label LIKE 'demo:%%' AND revoked_at IS NULL")
            return
        for role, (email, token) in TOKENS.items():
            uid = db.val(conn, "SELECT id FROM users WHERE email = %s", (email,))
            if uid:
                conn.execute("""INSERT INTO api_tokens (id, user_id, label, token_hash) VALUES (%s, %s, %s, %s)
                                ON CONFLICT (token_hash) DO UPDATE SET revoked_at = NULL""",
                             (new_id("tok"), uid, f"demo:{role}", digest(token)))
    print("\n".join(["=" * 64, f"DOGFOOD ready at {config.BASE_URL}  (demo mode: every seeded password is '{PASSWORD}')",
                     *[f"  {r:<12} Authorization: Bearer {t}" for r, (_, t) in TOKENS.items()], "=" * 64]), flush=True)
