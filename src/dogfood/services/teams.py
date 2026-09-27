"""Teams: create one, join with an invite code, leave. Team changes follow the submission window."""

from .. import audit, db
from ..errors import bad, conflict, forbidden, not_found
from ..security import new_id, new_token
from .submissions import closed, is_open, team_of


def _name(conn, event_id, name: str) -> str:
    name = " ".join((name or "").split())
    if not 2 <= len(name) <= 60:
        raise bad("team name must be 2 to 60 characters")
    if db.one(conn, "SELECT 1 FROM teams WHERE event_id = %s AND lower(name) = lower(%s)", (event_id, name)):
        raise conflict("another team in this event already uses that name", "team_name_taken")
    return name


def _roles_block(conn, user, ev):
    # Judges and organizers may not compete in the event they judge or run.
    if db.one(conn, "SELECT 1 FROM memberships WHERE event_id = %s AND user_id = %s AND role IN ('judge','organizer')",
              (ev["id"], user.id)):
        raise forbidden("judges and organizers of this event cannot join a team in it", "role_conflict")


def create(conn, user, ev, name: str) -> dict:
    if not is_open(ev):
        raise closed(ev)
    _roles_block(conn, user, ev)
    if team_of(conn, user.id, ev["id"]):
        raise conflict("you are already on a team in this event; leave it first", "already_on_team")
    tid = new_id("tm")
    conn.execute("INSERT INTO teams (id, event_id, name, invite_code) VALUES (%s, %s, %s, %s)",
                 (tid, ev["id"], _name(conn, ev["id"], name), new_token(9)))
    conn.execute("INSERT INTO team_members VALUES (%s, %s, %s, true)", (tid, ev["id"], user.id))
    conn.execute("INSERT INTO memberships VALUES (%s, %s, 'participant') ON CONFLICT DO NOTHING", (ev["id"], user.id))
    audit.log(conn, user, "team.create", ev["id"], tid)
    return db.one(conn, "SELECT * FROM teams WHERE id = %s", (tid,))


def join(conn, user, code: str) -> dict:
    team = db.one(conn, "SELECT * FROM teams WHERE invite_code = %s FOR UPDATE", (code.strip(),))
    if team is None:
        raise not_found("that invite code does not match any team", "bad_invite_code")
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s", (team["event_id"],))
    if not is_open(ev):
        raise closed(ev)
    _roles_block(conn, user, ev)
    current = team_of(conn, user.id, ev["id"])
    if current and current["id"] == team["id"]:
        return team
    if current:
        raise conflict(f"you are already on {current['name']}; leave it first", "already_on_team")
    # The FOR UPDATE on the team row above serializes concurrent joins, so two people cannot both
    # take the last seat.
    size = db.val(conn, "SELECT count(*) FROM team_members WHERE team_id = %s", (team["id"],))
    if size >= ev["max_team_size"]:
        raise conflict(f"{team['name']} is full ({ev['max_team_size']} members)", "team_full")
    conn.execute("INSERT INTO team_members VALUES (%s, %s, %s, false)", (team["id"], ev["id"], user.id))
    conn.execute("INSERT INTO memberships VALUES (%s, %s, 'participant') ON CONFLICT DO NOTHING", (ev["id"], user.id))
    audit.log(conn, user, "team.join", ev["id"], team["id"])
    return team


def leave(conn, user, ev) -> None:
    if not is_open(ev):
        raise closed(ev)
    team = team_of(conn, user.id, ev["id"])
    if team is None:
        raise bad("you are not on a team in this event")
    db.one(conn, "SELECT id FROM teams WHERE id = %s FOR UPDATE", (team["id"],))
    conn.execute("DELETE FROM team_members WHERE team_id = %s AND user_id = %s", (team["id"], user.id))
    left = db.rows(conn, "SELECT user_id, captain FROM team_members WHERE team_id = %s ORDER BY user_id", (team["id"],))
    if not left:
        # Last one out: the team and its draft go; a submitted entry is withdrawn, never deleted.
        conn.execute("UPDATE projects SET status = 'withdrawn' WHERE team_id = %s AND status = 'submitted'",
                     (team["id"],))
        conn.execute("DELETE FROM projects WHERE team_id = %s AND status = 'draft'", (team["id"],))
        if not db.one(conn, "SELECT 1 FROM projects WHERE team_id = %s", (team["id"],)):
            conn.execute("DELETE FROM teams WHERE id = %s", (team["id"],))
    elif not any(m["captain"] for m in left):
        conn.execute("UPDATE team_members SET captain = true WHERE team_id = %s AND user_id = %s",
                     (team["id"], left[0]["user_id"]))
    conn.execute("DELETE FROM memberships WHERE event_id = %s AND user_id = %s AND role = 'participant'",
                 (ev["id"], user.id))
    audit.log(conn, user, "team.leave", ev["id"], team["id"])


def rotate_code(conn, user, ev) -> str:
    team = team_of(conn, user.id, ev["id"])
    if team is None or not db.val(conn, "SELECT captain FROM team_members WHERE team_id=%s AND user_id=%s",
                                  (team["id"], user.id)):
        raise forbidden("only the team captain can change the invite code")
    code = new_token(9)
    conn.execute("UPDATE teams SET invite_code = %s WHERE id = %s", (code, team["id"]))
    audit.log(conn, user, "team.rotate_code", ev["id"], team["id"])
    return code
