"""People's-choice voting.

Rules, each enforced on the server (and the window also by the database):
  * voting is open for opens <= now < closes, decided by the database clock
  * one vote per voter per project (primary key); a budget of `votes_per_voter`
  * a voter never votes for their own team: membership *at the time of the vote* decides
  * only submitted projects can receive votes; if a project is withdrawn later its votes stay as
    history, are not tallied, and no longer count against the voter's budget
  * tallies are sealed until voting closes, for everyone, organizers and admins included
Concurrency: the voter row is locked FOR UPDATE, so 20 simultaneous votes with 3 left in the
budget give exactly 3 successes. The event row is read FOR SHARE, so a settings change cannot slip
between the check and the insert.
"""

import re
from datetime import datetime, timedelta, timezone

import psycopg

from .. import db
from ..errors import HTTPError, bad, conflict, forbidden, not_found
from ..security import digest, keyed, new_id, new_token

EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def state(ev) -> str:
    now = datetime.now(timezone.utc)
    if ev["voting_opens_at"] is None:
        return "off"
    if now < ev["voting_opens_at"]:
        return "not_yet"
    if ev["voting_closes_at"] is not None and now >= ev["voting_closes_at"]:
        return "closed"
    return "open"


def sealed(ev) -> bool:
    """Counts are hidden until voting has a close time and it has passed."""
    return ev["voting_opens_at"] is not None and (ev["voting_closes_at"] is None or state(ev) != "closed")


def user_voter(conn, event_id: str, user_id: str) -> str:
    conn.execute("INSERT INTO voters (id, event_id, user_id) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                 (new_id("vtr"), event_id, user_id))
    return db.val(conn, "SELECT id FROM voters WHERE event_id = %s AND user_id = %s", (event_id, user_id))


def email_key(email: str) -> str:
    return keyed(email.strip().lower())


def request_link(conn, ev, email: str, base_url: str) -> str:
    """Several links for one address all lead to the same voter, so extra links never mean extra votes."""
    email = (email or "").strip().lower()
    if not EMAIL.match(email):
        raise bad("enter a valid email address")
    if state(ev) != "open":
        raise forbidden("voting is not open", "voting_closed")
    k = email_key(email)
    conn.execute("INSERT INTO voters (id, event_id, email_key) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                 (new_id("vtr"), ev["id"], k))
    vid = db.val(conn, "SELECT id FROM voters WHERE event_id = %s AND email_key = %s", (ev["id"], k))
    raw = new_token(24)
    conn.execute("INSERT INTO voter_links VALUES (%s, %s, %s, NULL)",
                 (digest(raw), vid, datetime.now(timezone.utc) + timedelta(hours=24)))
    link = f"{base_url.rstrip('/')}/vote/{raw}"
    conn.execute("INSERT INTO outbox (to_address, subject, body) VALUES (%s, %s, %s)",
                 (email, f"Your voting link for {ev['name']}",
                  f"Open this link to vote in {ev['name']}. It works once and expires in 24 hours:\n\n{link}\n"))
    return link


def use_link(conn, raw: str) -> tuple[dict, str, str]:
    """Returns (voter, session token, csrf). A used, expired or altered link is a plain 404."""
    row = db.one(conn, """SELECT l.*, v.event_id FROM voter_links l JOIN voters v ON v.id = l.voter_id
                          WHERE l.token_hash = %s FOR UPDATE OF l""", (digest(raw),))
    if row is None or row["used_at"] or row["expires_at"] <= datetime.now(timezone.utc):
        raise not_found("this voting link is not valid (links work once and expire after 24 hours)", "bad_link")
    conn.execute("UPDATE voter_links SET used_at = now() WHERE token_hash = %s", (row["token_hash"],))
    session, csrf = new_token(32), new_token(16)
    conn.execute("INSERT INTO voter_sessions VALUES (%s, %s, %s, %s)",
                 (digest(session), row["voter_id"], csrf, datetime.now(timezone.utc) + timedelta(hours=12)))
    return row, session, csrf


def session_voter(conn, raw: str | None, event_id: str):
    if not raw:
        return None
    return db.one(conn, """SELECT v.*, s.csrf FROM voter_sessions s JOIN voters v ON v.id = s.voter_id
                           WHERE s.token_hash = %s AND s.expires_at > now() AND v.event_id = %s""",
                  (digest(raw), event_id))


def _own_team(conn, voter, project) -> bool:
    members = db.rows(conn, """SELECT u.id, u.email FROM team_members m JOIN users u ON u.id = m.user_id
                               WHERE m.team_id = %s""", (project["team_id"],))
    if voter["user_id"]:
        return any(m["id"] == voter["user_id"] for m in members)
    return any(email_key(m["email"]) == voter["email_key"] for m in members)


def used(conn, voter_id: str) -> int:
    return db.val(conn, """SELECT count(*) FROM votes v JOIN projects p ON p.id = v.project_id
                           WHERE v.voter_id = %s AND p.status = 'submitted'""", (voter_id,))


def cast(conn, voter_id: str, project_id: str) -> dict:
    voter = db.one(conn, "SELECT * FROM voters WHERE id = %s FOR UPDATE", (voter_id,))
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s FOR SHARE", (voter["event_id"],))
    if state(ev) != "open":
        raise forbidden("voting is not open", "voting_closed")
    p = db.one(conn, "SELECT * FROM projects WHERE id = %s AND event_id = %s", (project_id, ev["id"]))
    if p is None or p["status"] != "submitted":
        raise not_found("no such project on this ballot", "not_on_ballot")
    if _own_team(conn, voter, p):
        raise forbidden("you cannot vote for your own team", "own_team")
    if db.one(conn, "SELECT 1 FROM votes WHERE voter_id = %s AND project_id = %s", (voter_id, project_id)):
        return {"ok": True, "already": True, "left": ev["votes_per_voter"] - used(conn, voter_id)}
    n = used(conn, voter_id)
    if n >= ev["votes_per_voter"]:
        raise conflict(f"you have used all {ev['votes_per_voter']} votes; remove one to vote again", "no_votes_left")
    try:
        conn.execute("INSERT INTO votes (voter_id, project_id, event_id) VALUES (%s, %s, %s)",
                     (voter_id, project_id, ev["id"]))
    except psycopg.Error as e:
        if e.sqlstate == "DF005":
            raise forbidden("voting is not open", "voting_closed")
        raise
    # Votes are not in the audit log: the log is readable by organizers, and a ballot is private.
    return {"ok": True, "left": ev["votes_per_voter"] - n - 1}


def withdraw(conn, voter_id: str, project_id: str) -> dict:
    voter = db.one(conn, "SELECT * FROM voters WHERE id = %s FOR UPDATE", (voter_id,))
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s FOR SHARE", (voter["event_id"],))
    if state(ev) != "open":
        raise forbidden("voting is not open", "voting_closed")
    conn.execute("DELETE FROM votes WHERE voter_id = %s AND project_id = %s", (voter_id, project_id))
    return {"ok": True, "left": ev["votes_per_voter"] - used(conn, voter_id)}


def ballot(conn, ev, voter_id: str) -> list:
    """Every submitted project exactly once, in an order that is stable for this voter and different
    between voters: sorted by a keyed hash of (voter, project). Nobody can predict another voter's order,
    so position-one advantage is spread evenly instead of always going to the same project."""
    rows = db.rows(conn, """SELECT p.id, p.title, p.summary, t.name AS track, tm.name AS team,
                                   EXISTS (SELECT 1 FROM votes v WHERE v.voter_id = %s AND v.project_id = p.id) AS mine
                            FROM projects p JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks t ON t.id = p.track_id
                            WHERE p.event_id = %s AND p.status = 'submitted'""", (voter_id, ev["id"]))
    return sorted(rows, key=lambda r: keyed(f"{voter_id}:{r['id']}"))


def tally(conn, ev) -> list:
    if sealed(ev):
        raise HTTPError(403, "tallies_sealed", "vote counts stay sealed until voting closes")
    return db.rows(conn, """SELECT p.id AS project_id, p.title, tm.name AS team, count(v.voter_id) AS votes
                            FROM projects p JOIN teams tm ON tm.id = p.team_id
                            LEFT JOIN votes v ON v.project_id = p.id
                            WHERE p.event_id = %s AND p.status = 'submitted'
                            GROUP BY p.id, tm.name ORDER BY votes DESC, p.title""", (ev["id"],))
