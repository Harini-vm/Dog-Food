"""Import an event from JSON: the published fixtures.json format, or our own export (a superset).

The file is input, not our data model. Everything is validated *before* the first write, and the
whole import runs in the caller's transaction, so a bad file writes nothing.

Where the fixture leaves a decision open, the importer decides and reports it:
  * two live entries from one team: the earliest stays, later ones become 'duplicate'
    (an export states each project's status and duplicate_of explicitly, and those are kept)
  * two teams with the same name: names are unique per event, so the team id is appended
  * no rubric weights: functionality 0.40, quality 0.35, innovation 0.25
Importing an event id that already exists is refused (409) unless as_copy is set; a copy gets fresh
ids for everything and keeps people (matched by email).
"""

import hashlib
from collections import defaultdict
from datetime import datetime

from .. import audit, db
from ..errors import bad, conflict
from ..security import new_id, new_token

DEFAULT_WEIGHTS = {"functionality": 0.40, "quality": 0.35, "innovation": 0.25}
STATUSES = ("draft", "submitted", "withdrawn", "duplicate")


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


def _int(v, where, lo, hi):
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        raise bad(f"{where}: expected a whole number from {lo} to {hi}, got {v!r}")
    return v


def _str(v, where, maxlen, required=True):
    if v is None and not required:
        return None
    if not isinstance(v, str) or (required and not v.strip()) or len(v) > maxlen:
        raise bad(f"{where}: expected text of at most {maxlen} characters")
    return v


def validate(data) -> None:
    """Every check that can be made on the file alone. Runs before anything is written."""
    if not isinstance(data, dict) or not isinstance(data.get("event"), dict):
        raise bad("expected the fixtures.json format: an object with an 'event'")
    ev = data["event"]
    _str(ev.get("name", "Imported event"), "event.name", 120)
    _ts(ev.get("submissions_close"), "event.submissions_close")
    for section in ("tracks", "judges", "teams", "projects", "scores", "criteria", "assignments"):
        if not isinstance(data.get(section, []), list):
            raise bad(f"{section} must be a list")
    for section in ("tracks", "judges", "teams", "projects"):
        seen = set()
        for row in data.get(section, []):
            rid = row.get("id") if isinstance(row, dict) else None
            if not isinstance(rid, str) or not rid or len(rid) > 64 or rid in seen:
                raise bad(f"{section}: missing or duplicate id {rid!r}")
            seen.add(rid)
    tracks = {t["id"] for t in data.get("tracks", [])}
    for t in data.get("tracks", []):
        _str(t.get("name"), f"track {t['id']}", 80)
    emails = set()
    for j in data.get("judges", []):
        e = _str(j.get("email"), f"judge {j['id']} email", 200).strip().lower()
        if e in emails:
            raise bad(f"judge {j['id']}: email {e} is used by another judge")
        emails.add(e)
        for t in j.get("tracks", []) or []:
            if t not in tracks:
                raise bad(f"judge {j['id']}: unknown track {t!r}")
    teams = {t["id"] for t in data.get("teams", [])}
    for t in data.get("teams", []):
        _str(t.get("name"), f"team {t['id']} name", 60)
        if not isinstance(t.get("members", []), list) or not all(isinstance(m, str) for m in t.get("members", [])):
            raise bad(f"team {t['id']}: members must be a list of emails")
    keys = set()
    for c in data.get("criteria", []):
        k = _str(c.get("key"), "criterion key", 30)
        if k in keys:
            raise bad(f"criterion {k!r} appears twice")
        keys.add(k)
        lo, hi = _int(c.get("min", 1), f"criterion {k} min", 0, 99), _int(c.get("max", 5), f"criterion {k} max", 1, 100)
        if lo >= hi:
            raise bad(f"criterion {k}: min must be below max")
        w = c.get("weight", 1)
        if isinstance(w, bool) or not isinstance(w, (int, float)) or not 0 < w <= 100:
            raise bad(f"criterion {k}: weight must be above 0 and at most 100")
    scale = {c["key"]: (c.get("min", 1), c.get("max", 5)) for c in data.get("criteria", [])}
    projects = {}
    for p in data.get("projects", []):
        if p.get("team") not in teams:
            raise bad(f"project {p['id']}: unknown team {p.get('team')!r}")
        if p.get("track") is not None and p["track"] not in tracks:
            raise bad(f"project {p['id']}: unknown track {p['track']!r}")
        _str(p.get("title"), f"project {p['id']} title", 120)
        _str(p.get("summary", ""), f"project {p['id']} summary", 500, required=False)
        if "status" in p and p["status"] not in STATUSES:
            raise bad(f"project {p['id']}: unknown status {p['status']!r}")
        if p.get("submitted_at") is not None:
            _ts(p["submitted_at"], f"project {p['id']}")
        projects[p["id"]] = p
    for p in projects.values():
        if p.get("status") == "duplicate":
            target = projects.get(p.get("duplicate_of"))
            if target is None or target["id"] == p["id"] or target.get("status", "submitted") == "duplicate":
                raise bad(f"project {p['id']}: duplicate_of must point at another, non-duplicate project")
    judges = {j["id"] for j in data.get("judges", [])}
    pairs = set()
    for s in data.get("scores", []):
        if s.get("judge") not in judges or s.get("project") not in projects:
            raise bad(f"score {s.get('judge')}->{s.get('project')}: unknown judge or project")
        if (s["judge"], s["project"]) in pairs:
            raise bad(f"judge {s['judge']} reviewed {s['project']} twice")
        pairs.add((s["judge"], s["project"]))
        if not isinstance(s.get("criteria"), dict) or not s["criteria"]:
            raise bad(f"score {s['judge']}->{s['project']}: criteria must be an object")
        for k, v in s["criteria"].items():
            lo, hi = scale.get(k, (1, 5))
            _int(v, f"score {s['judge']}->{s['project']} {k}", lo, hi)
        _str(s.get("comment") or "", "score comment", 5000, required=False)
    for a in data.get("assignments", []):
        if a.get("judge") not in judges or a.get("project") not in projects:
            raise bad(f"assignment {a.get('judge')}->{a.get('project')}: unknown judge or project")
    settings = ev.get("settings") or {}
    if "max_team_size" in settings:
        _int(settings["max_team_size"], "max_team_size", 1, 20)
    if "reviews_per_project" in settings:
        _int(settings["reviews_per_project"], "reviews_per_project", 1, 20)


def _copy_ids(data: dict) -> dict:
    """Fresh ids for every event-scoped row. People keep their identity (matched by email)."""
    suffix = "_" + new_token(3).lower().replace("-", "x").replace("_", "x")
    remap = {}
    for section in ("tracks", "teams", "projects"):
        for row in data.get(section, []):
            remap[row["id"]] = row["id"][:56] + suffix
    ev = {**data["event"], "id": new_id("evt")}
    out = {**data, "event": ev}
    out["tracks"] = [{**t, "id": remap[t["id"]]} for t in data.get("tracks", [])]
    out["judges"] = [{**j, "tracks": [remap[t] for t in j.get("tracks", []) or []]} for j in data.get("judges", [])]
    out["teams"] = [{**t, "id": remap[t["id"]]} for t in data.get("teams", [])]
    out["projects"] = [{**p, "id": remap[p["id"]], "team": remap[p["team"]],
                        "track": remap.get(p.get("track")) if p.get("track") else None,
                        **({"duplicate_of": remap[p["duplicate_of"]]} if p.get("duplicate_of") else {})}
                       for p in data.get("projects", [])]
    out["scores"] = [{**s, "project": remap[s["project"]]} for s in data.get("scores", [])]
    out["assignments"] = [{**a, "project": remap[a["project"]]} for a in data.get("assignments", [])]
    return out


def import_event(conn, data: dict, actor=None, organizers=(), pw_hash=None, as_copy=False) -> dict:
    validate(data)
    if as_copy:
        data = _copy_ids(data)
    ev = data["event"]
    if db.one(conn, "SELECT 1 FROM events WHERE id = %s", (ev.get("id"),)):
        raise conflict(f"event {ev.get('id')} is already imported; import it as a copy instead", "already_imported")
    ids = [x["id"] for s in ("tracks", "teams", "projects") for x in data.get(s, [])]
    taken = db.rows(conn, """SELECT id FROM tracks WHERE id = ANY(%(i)s) UNION SELECT id FROM teams WHERE id = ANY(%(i)s)
                             UNION SELECT id FROM projects WHERE id = ANY(%(i)s)""", {"i": ids})
    if taken:
        raise conflict(f"ids already in use by another event (e.g. {taken[0]['id']}); import it as a copy",
                       "already_imported")
    conn.execute("SET LOCAL dogfood.importing = 'on'")
    report = {"duplicates": [], "renamed_teams": []}

    event_id = ev.get("id") or new_id("evt")
    slug = "-".join("".join(c if c.isalnum() else " " for c in ev.get("name", "event").lower()).split())[:50] or "event"
    base, n = slug, 2
    while db.one(conn, "SELECT 1 FROM events WHERE slug = %s", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    st = ev.get("settings") or {}
    conn.execute("""INSERT INTO events (id, slug, name, description, closes_at, opens_at, judging_closes_at,
                                        max_team_size, reviews_per_project)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                 (event_id, slug, ev.get("name", "Imported event"), (ev.get("description") or "")[:2000],
                  _ts(ev["submissions_close"], "event"),
                  _ts(st["opens_at"], "opens_at") if st.get("opens_at") else None,
                  _ts(st["judging_closes_at"], "judging_closes_at") if st.get("judging_closes_at") else None,
                  st.get("max_team_size", 4), st.get("reviews_per_project", 3)))
    for uid in organizers:
        conn.execute("INSERT INTO memberships VALUES (%s, %s, 'organizer') ON CONFLICT DO NOTHING", (event_id, uid))

    for t in data.get("tracks", []):
        conn.execute("INSERT INTO tracks (id, event_id, name) VALUES (%s, %s, %s)", (t["id"], event_id, t["name"]))

    judges = {}
    for j in data.get("judges", []):
        uid = ensure_user(conn, j["email"], j.get("name", ""), j["id"], pw_hash)
        judges[j["id"]] = uid
        conn.execute("INSERT INTO memberships VALUES (%s, %s, 'judge') ON CONFLICT DO NOTHING", (event_id, uid))
        for t in j.get("tracks", []) or []:
            conn.execute("INSERT INTO judge_tracks VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (event_id, uid, t))

    crit = {}
    keys = data.get("criteria") or [{"key": k} for k in dict.fromkeys(
        k for s in data.get("scores", []) for k in (s.get("criteria") or {}))]
    for i, c in enumerate(keys):
        cid = new_id("crt")
        weight = c.get("weight", DEFAULT_WEIGHTS.get(c["key"], 1.0))
        conn.execute("""INSERT INTO criteria (id, event_id, key, name, weight, min_score, max_score, position)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                     (cid, event_id, c["key"], c.get("name", c["key"].replace("_", " ").title()), weight,
                      c.get("min", 1), c.get("max", 5), i))
        crit[c["key"]] = cid
    report["weights"] = {c["key"]: c.get("weight", DEFAULT_WEIGHTS.get(c["key"], 1.0)) for c in keys}

    names = set()
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

    explicit = any("status" in p for p in data.get("projects", []))
    rows = []
    if explicit:
        for p in data.get("projects", []):
            rows.append((p, p.get("status", "submitted"), p.get("duplicate_of") if p.get("status") == "duplicate" else None))
    else:
        by_team = defaultdict(list)
        for p in data.get("projects", []):
            by_team[p["team"]].append(p)
        for entries in by_team.values():
            entries.sort(key=lambda p: (p.get("submitted_at") or "", p["id"]))
            for p in entries:
                dup = None if p is entries[0] else entries[0]["id"]
                rows.append((p, "duplicate" if dup else "submitted", dup))
    rows.sort(key=lambda r: r[1] == "duplicate")        # canonical rows first, so duplicate_of always resolves
    for p, status, dup in rows:
        conn.execute("""INSERT INTO projects (id, event_id, team_id, track_id, title, summary, repo_url, status,
                                              duplicate_of, submitted_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                     (p["id"], event_id, p["team"], p.get("track"), p["title"], p.get("summary", "") or "",
                      p.get("repo_url", "") or "", status, dup,
                      _ts(p["submitted_at"], p["id"]) if p.get("submitted_at") else None))
        if dup:
            report["duplicates"].append({"project": p["id"], "duplicate_of": dup})

    # Every score becomes an assignment plus the score: a score cannot exist without its assignment.
    for sc in data.get("scores", []):
        judge, pid = judges[sc["judge"]], sc["project"]
        aid, sid = new_id("asg"), new_id("scr")
        conn.execute("INSERT INTO assignments (id, event_id, judge_id, project_id, batch) VALUES (%s,%s,%s,%s,'import')",
                     (aid, event_id, judge, pid))
        conn.execute("INSERT INTO scores (id, assignment_id, event_id, judge_id, project_id, comment) "
                     "VALUES (%s,%s,%s,%s,%s,%s)", (sid, aid, event_id, judge, pid, sc.get("comment") or ""))
        for k, v in sc["criteria"].items():
            if k not in crit:
                raise bad(f"score {sc['judge']}->{pid}: unknown criterion {k!r}")
            conn.execute("INSERT INTO score_items VALUES (%s, %s, %s)", (sid, crit[k], v))
    scored = {(judges[s["judge"]], s["project"]) for s in data.get("scores", [])}
    for a in data.get("assignments", []):
        if (judges[a["judge"]], a["project"]) not in scored:
            conn.execute("INSERT INTO assignments (id, event_id, judge_id, project_id, batch) VALUES (%s,%s,%s,%s,'import') "
                         "ON CONFLICT DO NOTHING", (new_id("asg"), event_id, judges[a["judge"]], a["project"]))
    conn.execute("""INSERT INTO conflicts (event_id, judge_id, team_id, reason)
                    SELECT %s, m.user_id, tm.team_id, 'judge is on this team' FROM memberships m
                    JOIN team_members tm ON tm.user_id = m.user_id AND tm.event_id = m.event_id
                    WHERE m.event_id = %s AND m.role = 'judge' ON CONFLICT DO NOTHING""", (event_id, event_id))

    report.update(event_id=event_id, scores=len(data.get("scores", [])), slug=slug,
                  projects=len(data.get("projects", [])), teams=len(data.get("teams", [])),
                  judges=len(data.get("judges", [])))
    audit.log(conn, actor, "event.import", event_id, event_id, report)
    return report


def export_event(conn, event_id: str) -> dict:
    """Everything needed to rebuild the event elsewhere, in the format import_event reads."""
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s", (event_id,))
    iso = lambda t: t.isoformat().replace("+00:00", "Z") if t else None  # noqa: E731
    judges = db.rows(conn, """SELECT u.id, u.name, u.email, coalesce(array_agg(jt.track_id ORDER BY jt.track_id)
                                     FILTER (WHERE jt.track_id IS NOT NULL), '{}') AS tracks
                              FROM memberships m JOIN users u ON u.id = m.user_id
                              LEFT JOIN judge_tracks jt ON jt.user_id = u.id AND jt.event_id = m.event_id
                              WHERE m.event_id = %s AND m.role = 'judge' GROUP BY u.id ORDER BY u.id""", (event_id,))
    teams = db.rows(conn, """SELECT t.id, t.name, coalesce(array_agg(u.email ORDER BY m.captain DESC, u.email)
                                     FILTER (WHERE u.email IS NOT NULL), '{}') AS members
                             FROM teams t LEFT JOIN team_members m ON m.team_id = t.id LEFT JOIN users u ON u.id = m.user_id
                             WHERE t.event_id = %s GROUP BY t.id ORDER BY t.id""", (event_id,))
    projects = db.rows(conn, "SELECT * FROM projects WHERE event_id = %s ORDER BY id", (event_id,))
    scores = db.rows(conn, """SELECT s.judge_id, s.project_id, s.comment, jsonb_object_agg(c.key, i.value) AS criteria
                              FROM scores s JOIN score_items i ON i.score_id = s.id JOIN criteria c ON c.id = i.criterion_id
                              WHERE s.event_id = %s GROUP BY s.id ORDER BY s.judge_id, s.project_id""", (event_id,))
    open_asg = db.rows(conn, """SELECT a.judge_id, a.project_id FROM assignments a
                                WHERE a.event_id = %s AND NOT EXISTS (SELECT 1 FROM scores s WHERE s.assignment_id = a.id)
                                ORDER BY 1, 2""", (event_id,))
    return {
        "format": "dogfood-event/1",
        "event": {"id": ev["id"], "name": ev["name"], "description": ev["description"],
                  "submissions_close": iso(ev["closes_at"]),
                  "settings": {"opens_at": iso(ev["opens_at"]), "judging_closes_at": iso(ev["judging_closes_at"]),
                               "max_team_size": ev["max_team_size"], "reviews_per_project": ev["reviews_per_project"]}},
        "tracks": [{"id": t["id"], "name": t["name"]} for t in
                   db.rows(conn, "SELECT id, name FROM tracks WHERE event_id = %s ORDER BY id", (event_id,))],
        "criteria": [{"key": c["key"], "name": c["name"], "weight": float(c["weight"]), "min": c["min_score"],
                      "max": c["max_score"]} for c in
                     db.rows(conn, "SELECT * FROM criteria WHERE event_id = %s ORDER BY position", (event_id,))],
        "judges": [{"id": j["id"], "name": j["name"], "email": j["email"], "tracks": list(j["tracks"])} for j in judges],
        "teams": [{"id": t["id"], "name": t["name"], "members": list(t["members"])} for t in teams],
        "projects": [{"id": p["id"], "team": p["team_id"], "track": p["track_id"], "title": p["title"],
                      "summary": p["summary"], "repo_url": p["repo_url"], "status": p["status"],
                      "duplicate_of": p["duplicate_of"], "submitted_at": iso(p["submitted_at"])} for p in projects],
        "scores": [{"judge": s["judge_id"], "project": s["project_id"], "criteria": s["criteria"],
                    "comment": s["comment"]} for s in scores],
        "assignments": [{"judge": a["judge_id"], "project": a["project_id"]} for a in open_asg],
    }
