"""Building blocks for tests that need their own event, people and tokens."""

import secrets
from datetime import datetime, timedelta, timezone

import httpx

from conftest import BASE, ORGANIZER


def sql(q, args=None):
    from dogfood import db
    with db.tx() as conn:
        cur = conn.execute(q, args)
        return cur.fetchall() if cur.description else cur.rowcount


def client(token=None, **kw):
    return httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=20, **kw)


def token_for(email):
    from dogfood.security import digest, new_id
    raw = "t_" + secrets.token_hex(8)
    uid = sql("SELECT id FROM users WHERE email = %s", (email,))[0]["id"]
    sql("INSERT INTO api_tokens (id, user_id, label, token_hash) VALUES (%s, %s, 'test', %s)",
        (new_id("tok"), uid, digest(raw)))
    return raw


def person(email, name="Test Person"):
    from dogfood import ratelimit
    ratelimit.reset()
    r = client().post("/signup", json={"email": email, "name": name, "password": "longenough"})
    assert r.status_code == 303, r.text
    return token_for(email)


def iso(**delta):
    return (datetime.now(timezone.utc) + timedelta(**delta)).isoformat()


def event_with_entries(n_teams=4, **settings):
    """An open event with n submitted entries, one captain per team. Returns a dict."""
    tag = secrets.token_hex(3)
    r = client(ORGANIZER).post("/organize/events", json={"name": f"Test {tag}", "closes_at": iso(days=1),
                                                         "tracks": "Alpha", **settings})
    assert r.status_code == 201, r.text
    ev = r.json()
    teams = []
    for i in range(n_teams):
        email = f"cap{i}.{tag}@example.org"
        tok = person(email, f"Captain {i}")
        t = client(tok).post(f"/events/{ev['slug']}/teams", json={"name": f"Team {i} {tag}"}).json()
        pid = client(tok).post("/projects/new", json={"event": ev["slug"], "title": f"Entry {i} {tag}",
                                                      "action": "submit"}).json()["id"]
        teams.append({**t, "token": tok, "email": email, "project": pid})
    return {**ev, "tag": tag, "teams": teams}


def open_voting(ev, votes=3):
    r = client(ORGANIZER).post(f"/organize/{ev['slug']}/settings",
                               json={"voting_opens_at": iso(minutes=-1), "voting_closes_at": iso(hours=2),
                                     "votes_per_voter": votes})
    assert r.status_code == 200, r.text


def close_voting(ev):
    sql("UPDATE events SET voting_opens_at = now() - interval '2 hours', voting_closes_at = now() - interval '1 second' "
        "WHERE id = %s", (ev["id"],))
