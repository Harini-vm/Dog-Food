"""The authorization policy, in one place. Routes call these before they query anything.

Nothing here trusts the UI: the acceptance checker, and any attacker, arrive with curl.
"""

from . import db
from .errors import forbidden, not_found, unauthorized


def roles(conn, user, event_id) -> set[str]:
    if user is None:
        return set()
    r = {x["role"] for x in db.rows(conn, "SELECT role FROM memberships WHERE event_id = %s AND user_id = %s",
                                    (event_id, user.id))}
    return r | {"admin"} if user.is_admin else r


def need_login(user):
    if user is None:
        raise unauthorized()
    return user


def need_organizer(conn, user, event_id):
    need_login(user)
    if not roles(conn, user, event_id) & {"organizer", "admin"}:
        raise forbidden("organizers of this event only")
    return user


def need_judge(conn, user, event_id):
    need_login(user)
    if "judge" not in roles(conn, user, event_id):
        raise forbidden("judges of this event only")
    return user


def organized_events(conn, user) -> list[str]:
    if user.is_admin:
        return [r["id"] for r in db.rows(conn, "SELECT id FROM events")]
    return [r["event_id"] for r in db.rows(
        conn, "SELECT event_id FROM memberships WHERE user_id = %s AND role = 'organizer'", (user.id,))]


def score_scope(conn, user, judge_id: str) -> list[str]:
    """Which events' scores by `judge_id` may `user` read?

    - the judge themself: every event they judge
    - an organizer: the events they organize where that person judges (admins: all)
    - anyone else, including every other judge: 403, never an empty list, so a peer cannot even
      learn whether the other judge has scored anything
    """
    need_login(user)
    judged = [r["event_id"] for r in db.rows(
        conn, "SELECT event_id FROM memberships WHERE user_id = %s AND role = 'judge'", (judge_id,))]
    if user.id == judge_id:
        if not judged:
            raise forbidden("only judges have scores", "not_a_judge")
        return judged
    allowed = [e for e in judged if e in organized_events(conn, user)]
    if not allowed:
        raise forbidden("you may only read your own scores", "peer_scores_forbidden")
    return allowed


def project_visible(conn, user, project) -> bool:
    if project["status"] == "submitted":
        return True
    if user is None:
        return False
    return bool(db.one(conn, "SELECT 1 FROM team_members WHERE team_id = %s AND user_id = %s",
                       (project["team_id"], user.id))) or bool(roles(conn, user, project["event_id"]) & {"organizer", "admin"})


def missing():
    raise not_found("no such page")
