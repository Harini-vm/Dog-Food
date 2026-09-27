"""One full event, driven through the HTTP API the way people would: create, teams, submit, invite
judges, assign, score, publish. Plus the refusals at each step."""

import secrets
from datetime import datetime, timedelta, timezone

import pytest

from conftest import JUDGE_A, ORGANIZER, PARTICIPANT


def _token(sql, email):
    from dogfood.security import digest, new_id
    raw = "t_" + secrets.token_hex(8)
    uid = sql("SELECT id FROM users WHERE email = %s", (email,))[0]["id"]
    sql("INSERT INTO api_tokens (id, user_id, label, token_hash) VALUES (%s, %s, 'test', %s)",
        (new_id("tok"), uid, digest(raw)))
    return raw


def _person(api, sql, email, name="Test Person"):
    r = api().post("/signup", json={"email": email, "name": name, "password": "longenough"})
    assert r.status_code == 303, r.text
    return _token(sql, email)


@pytest.fixture(scope="module")
def world(server):
    """A fresh event with three teams, three entries and three judges."""
    import httpx

    from conftest import BASE
    from dogfood import db

    def sql(q, args=None):
        with db.tx() as conn:
            cur = conn.execute(q, args)
            return cur.fetchall() if cur.description else cur.rowcount

    def api(tok=None):
        return httpx.Client(base_url=BASE, headers={"Authorization": f"Bearer {tok}"} if tok else {}, timeout=15)

    tag = secrets.token_hex(3)
    closes = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    r = api(ORGANIZER).post("/organize/events", json={"name": f"Lifecycle {tag}", "closes_at": closes,
                                                      "tracks": "Alpha, Beta", "reviews_per_project": 2,
                                                      "max_team_size": 2})
    assert r.status_code == 201, r.text
    ev = r.json()
    tracks = {t["name"]: t["id"] for t in sql("SELECT id, name FROM tracks WHERE event_id = %s", (ev["id"],))}
    people, teams = {}, {}
    for i, track in enumerate(["Alpha", "Alpha", "Beta"]):
        tok = _person(api, sql, f"cap{i}.{tag}@example.org", f"Captain {i}")
        t = api(tok).post(f"/events/{ev['slug']}/teams", json={"name": f"Team {i} {tag}"})
        assert t.status_code == 201, t.text
        teams[i] = t.json()
        people[i] = tok
        s = api(tok).post("/projects/new", json={"event": ev["slug"], "title": f"Entry {i}", "track_id": tracks[track],
                                                  "action": "submit"})
        assert s.status_code == 201, s.text
        teams[i]["project"] = s.json()["id"]
    judges = {}
    for i, t in enumerate([["Alpha"], ["Beta"], []]):
        out = api(ORGANIZER).post(f"/organize/{ev['slug']}/people",
                                  json={"email": f"judge{i}.{tag}@example.org", "role": "judge",
                                        "tracks": [tracks[x] for x in t]})
        assert out.status_code == 201 and out.json()["link"].startswith("/invite/"), out.text
        judges[i] = {**out.json(), "token": _token(sql, f"judge{i}.{tag}@example.org")}
    return {"ev": ev, "tag": tag, "tracks": tracks, "people": people, "teams": teams, "judges": judges,
            "sql": sql, "api": api}


def test_only_organizers_create_events(api):
    r = api(PARTICIPANT).post("/organize/events", json={"name": "Nope", "closes_at": "2030-01-01T00:00"})
    assert r.status_code == 403


def test_other_organizers_are_locked_out(world, api):
    w = world
    tok = _person(api, w["sql"], f"org2.{w['tag']}@example.org")
    w["sql"]("INSERT INTO memberships SELECT 'evt_01', id, 'organizer' FROM users WHERE email = %s",
             (f"org2.{w['tag']}@example.org",))
    assert api(tok).get("/organize/evt_01").status_code == 200
    assert api(tok).get(f"/organize/{w['ev']['slug']}").status_code == 403
    assert api(JUDGE_A).get(f"/organize/{w['ev']['slug']}").status_code == 403


def test_team_rules(world, api):
    w = world
    code = w["teams"][0]["invite_code"]
    mate = _person(api, w["sql"], f"mate.{w['tag']}@example.org")
    assert api(mate).post(f"/join/{code}").status_code == 200
    extra = _person(api, w["sql"], f"extra.{w['tag']}@example.org")
    r = api(extra).post(f"/join/{code}")
    assert r.status_code == 409 and r.json()["error"] == "team_full"          # max_team_size = 2
    r = api(mate).post(f"/join/{w['teams'][1]['invite_code']}")
    assert r.status_code == 409 and r.json()["error"] == "already_on_team"
    r = api(w["judges"][0]["token"]).post(f"/join/{w['teams'][2]['invite_code']}")
    assert r.status_code == 403 and r.json()["error"] == "role_conflict"
    r = api(extra).post(f"/events/{w['ev']['slug']}/teams", json={"name": w["teams"][0]["name"].upper()})
    assert r.status_code == 409 and r.json()["error"] == "team_name_taken"   # names unique, case-insensitive


def test_invited_email_cannot_be_claimed_by_signup(world, api):
    email = f"judge0.{world['tag']}@example.org"
    world["sql"]("UPDATE users SET password_hash = NULL WHERE email = %s", (email,))
    r = api().post("/signup", json={"email": email, "name": "Impostor", "password": "longenough"})
    assert r.status_code == 409 and r.json()["error"] == "use_invite"


def test_planner_respects_tracks_and_is_rerunnable(world, api):
    w = world
    slug = w["ev"]["slug"]
    first = api(ORGANIZER).post(f"/organize/{slug}/assign").json()
    assert first["created"] == 6 and not first["shortfalls"]                   # 3 projects x 2 reviews
    rows = w["sql"]("""SELECT a.judge_id, p.track_id FROM assignments a JOIN projects p ON p.id = a.project_id
                       WHERE a.event_id = %s""", (w["ev"]["id"],))
    j0, j1 = w["judges"][0]["user_id"], w["judges"][1]["user_id"]
    assert all(r["track_id"] == w["tracks"]["Alpha"] for r in rows if r["judge_id"] == j0)
    assert all(r["track_id"] == w["tracks"]["Beta"] for r in rows if r["judge_id"] == j1)
    again = api(ORGANIZER).post(f"/organize/{slug}/assign").json()
    assert again["created"] == 0                                               # fills gaps only


def test_publish_rules_and_frozen_results(world, api):
    import psycopg

    w = world
    slug, eid = w["ev"]["slug"], w["ev"]["id"]
    assert api(ORGANIZER).post(f"/organize/{slug}/publish").json()["error"] == "still_open"
    # Judges score everything they were given.
    for j in w["judges"].values():
        for a in api(j["token"]).get("/api/v1/judge/assignments").json()["assignments"]:
            if a["event_id"] == eid:
                r = api(j["token"]).put(f"/api/v1/assignments/{a['id']}/score",
                                        json={"criteria": {"functionality": 4, "quality": 3, "innovation": 5}})
                assert r.status_code == 200, r.text
    w["sql"]("UPDATE events SET closes_at = now() - interval '1 minute' WHERE id = %s", (eid,))
    assert api().get(f"/api/v1/events/{slug}/results").status_code == 404      # not public before publishing
    r = api(ORGANIZER).post(f"/organize/{slug}/publish")
    assert r.status_code == 201, r.text
    pub = api().get(f"/api/v1/events/{slug}/results").json()
    assert pub["version"] == 1 and len(pub["ranking"]) == 3 and "judges" not in pub
    # Scores are locked: by the application...
    j = w["judges"][2]
    a = [x for x in api(j["token"]).get("/api/v1/judge/assignments").json()["assignments"] if x["event_id"] == eid][0]
    r = api(j["token"]).put(f"/api/v1/assignments/{a['id']}/score",
                            json={"criteria": {"functionality": 1, "quality": 1, "innovation": 1}})
    assert r.status_code == 403 and r.json()["error"] == "judging_closed"
    # ...and by the database, even for code that skips the check.
    with pytest.raises(psycopg.Error) as e:
        w["sql"]("UPDATE score_items SET value = 1 WHERE score_id IN (SELECT id FROM scores WHERE event_id = %s)", (eid,))
    assert e.value.sqlstate == "DF004"
    with pytest.raises(psycopg.Error) as e:
        w["sql"]("UPDATE results SET body = '{}' WHERE event_id = %s", (eid,))
    assert e.value.sqlstate == "DF003"
    # Retracting needs a reason, then reopens judging.
    assert api(ORGANIZER).post(f"/organize/{slug}/retract", json={"reason": ""}).status_code == 400
    assert api(ORGANIZER).post(f"/organize/{slug}/retract", json={"reason": "late review found"}).status_code == 200
    assert api().get(f"/api/v1/events/{slug}/results").status_code == 404
    r = api(j["token"]).put(f"/api/v1/assignments/{a['id']}/score",
                            json={"criteria": {"functionality": 2, "quality": 2, "innovation": 2}})
    assert r.status_code == 200


def test_moderation_after_deadline_is_status_only(world, api):
    w = world
    slug, pid = w["ev"]["slug"], w["teams"][2]["project"]
    r = api(ORGANIZER).post(f"/organize/{slug}/projects/{pid}/status", json={"status": "withdrawn", "reason": "test"})
    assert r.status_code == 200
    assert api().get(f"/projects/{pid}").status_code == 404                   # gone from the public site
    assert api(ORGANIZER).post(f"/organize/{slug}/projects/{pid}/status", json={"status": "submitted"}).status_code == 200
    r = api(w["people"][2]).post("/projects/new", json={"event": slug, "title": "Changed after deadline"})
    assert r.status_code == 403 and r.json()["error"] == "submissions_closed"
