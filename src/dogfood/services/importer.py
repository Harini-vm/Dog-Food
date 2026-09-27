"""Import an event from the published fixtures.json format.

The file is input, not our data model. Where it leaves a decision open, the importer decides and
reports it:
  * two live entries from one team: the earliest stays, later ones become 'duplicate'
  * two teams with the same name: names are unique per event, so the team id is appended
Everything runs in the caller's transaction, so a bad file writes nothing.
"""

import hashlib
from collections import defaultdict
from datetime import datetime

from .. import audit, db
from ..errors import bad, conflict
from ..security import new_id, new_token


def _ts(v, where):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        raise bad(f"{where}: not an ISO 8601 time: {v!r}")


def _uid(email: str) -> str:
    return "usr_" + hashlib.sha256(email.encode()).hexdigest()[:10]


def ensure_user(conn, email: str, name: str = "", wanted_id: str | None = None, pw_hash=None) -> str:
    email = email.strip().lower()
    if row := db.one(conn, "SELECT id FROM users WHERE email = %s", (email,)):
        return row["id"]
    uid = wanted_id if wanted_id and not db.one(conn, "SELECT 1 FROM users WHERE id = %s", (wanted_id,)) else _uid(email)
    conn.execute("INSERT INTO users (id, email, name, password_hash) VALUES (%s, %s, %s, %s)",
                 (uid, email, name or email.split("@")[0], pw_hash))
    return uid


def _check_ids(data: dict) -> None:
    for section in ("tracks", "judges", "teams", "projects"):
        seen = set()
        for row in data.get(section, []):
            rid = row.get("id")
            if not isinstance(rid, str) or rid in seen:
                raise bad(f"{section}: missing or duplicate id {rid!r}")
            seen.add(rid)


def import_event(conn, data: dict, actor=None, organizers=(), pw_hash=None) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("event"), dict):
        raise bad("expected the fixtures.json format: an object with an 'event'")
    _check_ids(data)
    ev = data["event"]
    if db.one(conn, "SELECT 1 FROM events WHERE id = %s", (ev.get("id"),)):
        raise conflict(f"event {ev.get('id')} is already imported")
    conn.execute("SET LOCAL dogfood.importing = 'on'")
    report = {"duplicates": [], "renamed_teams": []}

    event_id = ev.get("id") or new_id("evt")
    slug = "-".join("".join(c if c.isalnum() else " " for c in ev.get("name", "event").lower()).split())[:50]
    while db.one(conn, "SELECT 1 FROM events WHERE slug = %s", (slug,)):
        slug += "-2"
    conn.execute("INSERT INTO events (id, slug, name, closes_at) VALUES (%s, %s, %s, %s)",
                 (event_id, slug, ev.get("name", "Imported event"), _ts(ev.get("submissions_close"), "event")))
    for uid in organizers:
        conn.execute("INSERT INTO memberships VALUES (%s, %s, 'organizer') ON CONFLICT DO NOTHING", (event_id, uid))

    tracks = {t["id"] for t in data.get("tracks", [])}
    for t in data.get("tracks", []):
        conn.execute("INSERT INTO tracks (id, event_id, name) VALUES (%s, %s, %s)", (t["id"], event_id, t["name"]))

    for j in data.get("judges", []):
        uid = ensure_user(conn, j["email"], j.get("name", ""), j["id"], pw_hash)
        conn.execute("INSERT INTO memberships VALUES (%s, %s, 'judge') ON CONFLICT DO NOTHING", (event_id, uid))

    names = set()
    teams = {t["id"] for t in data.get("teams", [])}
    for t in data.get("teams", []):
        name = t["name"]
        if name.lower() in names:
            report["renamed_teams"].append(t["id"])
            name = f"{name} ({t['id']})"
        names.add(name.lower())
        conn.execute("INSERT INTO teams (id, event_id, name, invite_code) VALUES (%s, %s, %s, %s)",
                     (t["id"], event_id, name, new_token(9)))
        for i, email in enumerate(t.get("members", [])):
            uid = ensure_user(conn, email, pw_hash=pw_hash)
            conn.execute("INSERT INTO team_members VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                         (t["id"], event_id, uid, i == 0))
            conn.execute("INSERT INTO memberships VALUES (%s, %s, 'participant') ON CONFLICT DO NOTHING",
                         (event_id, uid))

    by_team = defaultdict(list)
    for p in data.get("projects", []):
        if p.get("team") not in teams:
            raise bad(f"project {p['id']}: unknown team {p.get('team')!r}")
        if p.get("track") is not None and p["track"] not in tracks:
            raise bad(f"project {p['id']}: unknown track {p['track']!r}")
        by_team[p["team"]].append(p)
    for team, entries in by_team.items():
        entries.sort(key=lambda p: (p.get("submitted_at") or "", p["id"]))
        keep = entries[0]
        for p in entries:                       # canonical first, so duplicate_of always points at a stored row
            dup = None if p is keep else keep["id"]
            conn.execute("""INSERT INTO projects (id, event_id, team_id, track_id, title, summary, repo_url, status,
                                                  duplicate_of, submitted_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                         (p["id"], event_id, team, p.get("track"), p["title"], p.get("summary", ""),
                          p.get("repo_url", ""), "duplicate" if dup else "submitted", dup,
                          _ts(p.get("submitted_at"), p["id"])))
            if dup:
                report["duplicates"].append({"project": p["id"], "duplicate_of": dup})

    report.update(event_id=event_id, slug=slug, projects=len(data.get("projects", [])),
                  teams=len(teams), judges=len(data.get("judges", [])))
    audit.log(conn, actor, "event.import", event_id, event_id, report)
    return report
