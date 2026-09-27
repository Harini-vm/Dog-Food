"""Teams and entries: draft, submit, edit until the deadline."""

from datetime import datetime, timezone

import psycopg

from .. import audit, db
from ..errors import bad, forbidden
from ..security import new_id


def is_open(ev) -> bool:
    """Same rule as the database trigger, checked first so users get a friendly message."""
    now = datetime.now(timezone.utc)
    return (ev["opens_at"] is None or now >= ev["opens_at"]) and now < ev["closes_at"]


def closed(ev):
    return forbidden(f"Submissions for {ev['name']} closed at {ev['closes_at']:%Y-%m-%d %H:%M} UTC.",
                     "submissions_closed")


def team_of(conn, user_id, event_id):
    return db.one(conn, """SELECT t.* FROM team_members m JOIN teams t ON t.id = m.team_id
                           WHERE m.user_id = %s AND m.event_id = %s""", (user_id, event_id))


def save(conn, user, ev, data: dict, submit: bool) -> str:
    if not is_open(ev):
        raise closed(ev)
    team = team_of(conn, user.id, ev["id"])
    if team is None:
        raise forbidden("join or create a team first", "no_team")
    title = (data.get("title") or "").strip()
    if not 1 <= len(title) <= 120:
        raise bad("title must be 1 to 120 characters")
    fields = {"title": title, "summary": (data.get("summary") or "").strip()[:500],
              "repo_url": (data.get("repo_url") or "").strip(), "track_id": data.get("track_id") or None}
    if fields["repo_url"] and not fields["repo_url"].startswith(("http://", "https://")):
        raise bad("repository URL must start with http:// or https://")
    live = db.one(conn, "SELECT * FROM projects WHERE team_id = %s AND status IN ('draft','submitted') FOR UPDATE",
                  (team["id"],))
    status = "submitted" if submit or (live and live["status"] == "submitted") else "draft"
    try:
        if live:
            pid = live["id"]
            conn.execute("""UPDATE projects SET title=%(title)s, summary=%(summary)s, repo_url=%(repo_url)s,
                              track_id=%(track_id)s, status=%(status)s, updated_at=now(),
                              submitted_at = CASE WHEN %(status)s='submitted' THEN coalesce(submitted_at, now()) END
                            WHERE id=%(id)s""", {**fields, "status": status, "id": pid})
        else:
            pid = new_id("prj")
            conn.execute("""INSERT INTO projects (id, event_id, team_id, track_id, title, summary, repo_url, status,
                                                  submitted_at)
                            VALUES (%(id)s, %(ev)s, %(team)s, %(track_id)s, %(title)s, %(summary)s, %(repo_url)s,
                                    %(status)s, CASE WHEN %(status)s='submitted' THEN now() END)""",
                         {**fields, "status": status, "id": pid, "ev": ev["id"], "team": team["id"]})
    except psycopg.Error as e:
        if e.sqlstate == "DF001":                 # the database said no: deadline passed mid-request
            raise closed(ev)
        raise
    audit.log(conn, user, f"project.{status}", ev["id"], pid, {"title": title})
    if status == "submitted" and not (live and live["status"] == "submitted"):
        from . import webhooks
        webhooks.emit(conn, ev["id"], "project.submitted", {"project_id": pid, "title": title, "team": team["name"]})
    return pid
