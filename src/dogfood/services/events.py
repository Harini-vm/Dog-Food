"""Creating and configuring events: settings, rubric, judges and co-organizers."""

import re
from datetime import datetime, timedelta, timezone

from .. import audit, db
from ..errors import bad, conflict
from ..security import digest, new_id, new_token
from .importer import DEFAULT_WEIGHTS, ensure_user

DEFAULT_RUBRIC = [("functionality", "Functionality"), ("quality", "Quality"), ("innovation", "Innovation")]
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def parse_time(value, field: str, required: bool = False):
    """Accepts ISO 8601 or the browser's datetime-local value. Times without an offset are UTC."""
    if value in (None, ""):
        if required:
            raise bad(f"{field} is required")
        return None
    try:
        t = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        raise bad(f"{field}: use a date and time like 2026-09-29T18:00")
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _int(value, field, lo, hi, default):
    if value in (None, ""):
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise bad(f"{field} must be a whole number")
    if not lo <= n <= hi:
        raise bad(f"{field} must be between {lo} and {hi}")
    return n


def slugify(name: str) -> str:
    s = "-".join("".join(c if c.isalnum() else " " for c in name.lower()).split())[:50]
    return s or "event"


def create(conn, user, data: dict) -> dict:
    name = (data.get("name") or "").strip()
    if not 3 <= len(name) <= 120:
        raise bad("event name must be 3 to 120 characters")
    closes = parse_time(data.get("closes_at"), "submission deadline", required=True)
    opens = parse_time(data.get("opens_at"), "opening time")
    if opens and opens >= closes:
        raise bad("submissions must open before they close")
    slug = slugify(data.get("slug") or name)
    base, n = slug, 2
    while db.one(conn, "SELECT 1 FROM events WHERE slug = %s", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    eid = new_id("evt")
    conn.execute("""INSERT INTO events (id, slug, name, description, opens_at, closes_at, max_team_size,
                                        reviews_per_project, created_by)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                 (eid, slug, name, (data.get("description") or "").strip()[:2000], opens, closes,
                  _int(data.get("max_team_size"), "team size", 1, 20, 4),
                  _int(data.get("reviews_per_project"), "reviews per project", 1, 20, 3), user.id))
    conn.execute("INSERT INTO memberships VALUES (%s, %s, 'organizer')", (eid, user.id))
    for i, (key, label) in enumerate(DEFAULT_RUBRIC):
        conn.execute("INSERT INTO criteria (id, event_id, key, name, weight, position) VALUES (%s,%s,%s,%s,%s,%s)",
                     (new_id("crt"), eid, key, label, DEFAULT_WEIGHTS[key], i))
    for t in [x.strip() for x in (data.get("tracks") or "").replace("\n", ",").split(",") if x.strip()][:30]:
        conn.execute("INSERT INTO tracks (id, event_id, name) VALUES (%s, %s, %s)", (new_id("trk"), eid, t[:80]))
    audit.log(conn, user, "event.create", eid, eid, {"name": name, "slug": slug})
    return db.one(conn, "SELECT * FROM events WHERE id = %s", (eid,))


def update_settings(conn, user, ev, data: dict) -> None:
    closes = parse_time(data.get("closes_at"), "submission deadline") or ev["closes_at"]
    opens = parse_time(data.get("opens_at"), "opening time") if "opens_at" in data else ev["opens_at"]
    judging = parse_time(data.get("judging_closes_at"), "judging deadline") if "judging_closes_at" in data \
        else ev["judging_closes_at"]
    if opens and opens >= closes:
        raise bad("submissions must open before they close")
    if judging and judging <= closes:
        raise bad("judging must close after submissions close")
    v_open = parse_time(data.get("voting_opens_at"), "voting opens") if "voting_opens_at" in data else ev["voting_opens_at"]
    v_close = parse_time(data.get("voting_closes_at"), "voting closes") if "voting_closes_at" in data \
        else ev["voting_closes_at"]
    if v_open and v_close and v_open >= v_close:
        raise bad("voting must open before it closes")
    if v_close and not v_open:
        raise bad("set when voting opens too")
    new = {"name": (data.get("name") or ev["name"]).strip()[:120],
           "description": (data.get("description", ev["description"]) or "").strip()[:2000],
           "opens_at": opens, "closes_at": closes, "judging_closes_at": judging,
           "max_team_size": _int(data.get("max_team_size"), "team size", 1, 20, ev["max_team_size"]),
           "reviews_per_project": _int(data.get("reviews_per_project"), "reviews per project", 1, 20,
                                       ev["reviews_per_project"]),
           "voting_opens_at": v_open, "voting_closes_at": v_close,
           "votes_per_voter": _int(data.get("votes_per_voter"), "votes per voter", 1, 50, ev["votes_per_voter"]),
           "comments_open": (str(data.get("comments_open", "")).lower() in ("1", "true", "on", "yes"))
                            if ("comments_open" in data or "settings_form" in data) else ev["comments_open"]}
    biggest = db.val(conn, "SELECT coalesce(max(n), 0) FROM (SELECT count(*) n FROM team_members "
                           "WHERE event_id = %s GROUP BY team_id) x", (ev["id"],))
    if new["max_team_size"] < biggest:
        raise bad(f"a team already has {biggest} members; the limit cannot go below that")
    conn.execute("""UPDATE events SET name=%(name)s, description=%(description)s, opens_at=%(opens_at)s,
                      closes_at=%(closes_at)s, judging_closes_at=%(judging_closes_at)s,
                      max_team_size=%(max_team_size)s, reviews_per_project=%(reviews_per_project)s,
                      voting_opens_at=%(voting_opens_at)s, voting_closes_at=%(voting_closes_at)s,
                      votes_per_voter=%(votes_per_voter)s, comments_open=%(comments_open)s
                    WHERE id=%(id)s""", {**new, "id": ev["id"]})
    changed = {k: str(v) for k, v in new.items() if v != ev[k]}
    audit.log(conn, user, "event.settings", ev["id"], ev["id"], changed)


def update_rubric(conn, user, ev, data: dict) -> None:
    """Weights can change any time before results are published (results are recomputed).
    Names too. The scale (min/max) cannot change once anyone has scored: old scores would be
    on a different scale from new ones."""
    if ev["results_published_at"]:
        raise conflict("results are published; retract them before changing the rubric")
    rubric = db.rows(conn, "SELECT * FROM criteria WHERE event_id = %s ORDER BY position FOR UPDATE", (ev["id"],))
    scored = db.val(conn, "SELECT count(*) FROM scores WHERE event_id = %s", (ev["id"],))
    changes = {}
    for c in rubric:
        k = c["key"]
        try:
            w = float(data.get(f"weight_{k}", c["weight"]))
        except (TypeError, ValueError):
            raise bad(f"weight for {c['name']} must be a number")
        if not 0 < w <= 100:
            raise bad(f"weight for {c['name']} must be above 0 and at most 100")
        name = (data.get(f"name_{k}") or c["name"]).strip()[:60]
        lo = _int(data.get(f"min_{k}"), "minimum", 0, 99, c["min_score"])
        hi = _int(data.get(f"max_{k}"), "maximum", 1, 100, c["max_score"])
        if lo >= hi:
            raise bad(f"{name}: minimum must be below maximum")
        if scored and (lo, hi) != (c["min_score"], c["max_score"]):
            raise conflict(f"{scored} reviews already use the {c['min_score']}–{c['max_score']} scale; "
                           "the scale can no longer change")
        if (w, name, lo, hi) != (float(c["weight"]), c["name"], c["min_score"], c["max_score"]):
            changes[k] = {"weight": w, "name": name, "min": lo, "max": hi}
            conn.execute("UPDATE criteria SET weight=%s, name=%s, min_score=%s, max_score=%s WHERE id=%s",
                         (w, name, lo, hi, c["id"]))
    new_key = (data.get("new_key") or "").strip().lower()
    if new_key:
        if scored:
            raise conflict("reviews already exist; adding a criterion would leave them incomplete")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,30}", new_key):
            raise bad("criterion key: lowercase letters, digits and _")
        conn.execute("INSERT INTO criteria (id, event_id, key, name, weight, position) VALUES (%s,%s,%s,%s,%s,%s)",
                     (new_id("crt"), ev["id"], new_key, new_key.replace("_", " ").title(), 1.0, len(rubric)))
        changes[new_key] = "added"
    if changes:
        audit.log(conn, user, "rubric.update", ev["id"], ev["id"], changes)


def invite(conn, user, ev, email: str, role: str, tracks=(), name: str = "") -> dict:
    """Adds the person with the role straight away and returns a one-time link to set a password.
    No mail server is needed: the organizer copies the link. An existing account just gets the role."""
    email = (email or "").strip().lower()
    if not EMAIL.match(email):
        raise bad("enter a valid email address")
    if role not in ("judge", "organizer"):
        raise bad("role must be judge or organizer")
    uid = ensure_user(conn, email, name.strip())
    conn.execute("INSERT INTO memberships VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (ev["id"], uid, role))
    known = {t["id"] for t in db.rows(conn, "SELECT id FROM tracks WHERE event_id = %s", (ev["id"],))}
    if role == "judge":
        for t in tracks:
            if t not in known:
                raise bad(f"unknown track {t!r}")
            conn.execute("INSERT INTO judge_tracks VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (ev["id"], uid, t))
        refresh_conflicts(conn, ev["id"])
    link = None
    if not db.val(conn, "SELECT password_hash FROM users WHERE id = %s", (uid,)):
        raw = new_token(24)
        conn.execute("INSERT INTO invitations VALUES (%s, %s, %s, %s, %s, now(), %s, NULL)",
                     (digest(raw), ev["id"], uid, role, user.id, datetime.now(timezone.utc) + timedelta(days=14)))
        link = f"/invite/{raw}"
    audit.log(conn, user, f"{role}.invite", ev["id"], uid, {"tracks": list(tracks)})
    return {"user_id": uid, "email": email, "role": role, "link": link}


def remove_judge(conn, user, ev, uid: str) -> dict:
    """Unscored assignments go back to the pool; finished reviews stay (and still count)."""
    conn.execute("DELETE FROM memberships WHERE event_id = %s AND user_id = %s AND role = 'judge'", (ev["id"], uid))
    freed = conn.execute("""DELETE FROM assignments a WHERE a.event_id = %s AND a.judge_id = %s
                              AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.assignment_id = a.id)""",
                         (ev["id"], uid)).rowcount
    conn.execute("DELETE FROM judge_tracks WHERE event_id = %s AND user_id = %s", (ev["id"], uid))
    audit.log(conn, user, "judge.remove", ev["id"], uid, {"freed_assignments": freed})
    return {"freed": freed}


def refresh_conflicts(conn, event_id: str) -> None:
    """A judge who is also on a team never reviews that team."""
    conn.execute("""INSERT INTO conflicts (event_id, judge_id, team_id, reason)
                    SELECT %s, m.user_id, tm.team_id, 'judge is on this team' FROM memberships m
                    JOIN team_members tm ON tm.user_id = m.user_id AND tm.event_id = m.event_id
                    WHERE m.event_id = %s AND m.role = 'judge' ON CONFLICT DO NOTHING""", (event_id, event_id))


def accept_invite(conn, raw: str, name: str, password: str):
    from ..security import hash_password
    inv = db.one(conn, "SELECT * FROM invitations WHERE token_hash = %s FOR UPDATE", (digest(raw),))
    if inv is None or inv["used_at"] or inv["expires_at"] < datetime.now(timezone.utc):
        raise bad("this invitation link is invalid, used or expired; ask the organizer for a new one", "bad_invite")
    if len(password) < 8:
        raise bad("password must be at least 8 characters")
    conn.execute("UPDATE users SET password_hash = %s, name = coalesce(nullif(%s, ''), name) WHERE id = %s",
                 (hash_password(password), name.strip()[:80], inv["user_id"]))
    conn.execute("UPDATE invitations SET used_at = now() WHERE token_hash = %s", (inv["token_hash"],))
    audit.log(conn, None, "invite.accept", inv["event_id"], inv["user_id"])
    return inv
