"""Where features meet: removed judges, late team changes, and UI-only rules attacked directly."""

from concurrent.futures import ThreadPoolExecutor

from conftest import ORGANIZER
from helpers import client, event_with_entries, open_voting, person, sql, token_for


def test_removed_judge_keeps_reviews_but_loses_access(server):
    e = event_with_entries(2, reviews_per_project=1)
    j = client(ORGANIZER).post(f"/organize/{e['slug']}/people", json={"email": f"x5.{e['tag']}@example.org"}).json()
    jt = token_for(f"x5.{e['tag']}@example.org")
    client(ORGANIZER).post(f"/organize/{e['slug']}/assign")
    a = client(jt).get("/api/v1/judge/assignments").json()["assignments"]
    score = {"criteria": {"functionality": 3, "quality": 3, "innovation": 3}}
    assert client(jt).put(f"/api/v1/assignments/{a[0]['id']}/score", json=score).status_code == 200
    out = client(ORGANIZER).post(f"/organize/{e['slug']}/judges/{j['user_id']}/remove").json()
    assert out["freed"] == 1                                                   # the unscored one goes back
    r = client(jt).put(f"/api/v1/assignments/{a[0]['id']}/score", json=score)
    assert r.status_code == 403 and r.json()["error"] == "not_a_judge"         # cannot edit old reviews
    assert sql("SELECT count(*) AS n FROM scores WHERE judge_id = %s", (j["user_id"],))[0]["n"] == 1   # kept
    acts = [r["action"] for r in sql("SELECT action FROM audit_log WHERE event_id = %s", (e["id"],))]
    assert "judge.remove" in acts


def test_teams_lock_at_the_deadline(server):
    e = event_with_entries(1)
    sql("UPDATE events SET closes_at = now() - interval '1 second' WHERE id = %s", (e["id"],))
    late = person(f"x7.{e['tag']}@example.org")
    r = client(late).post(f"/join/{e['teams'][0]['invite_code']}")
    assert r.status_code == 403 and r.json()["error"] == "submissions_closed"
    r = client(e["teams"][0]["token"]).post(f"/events/{e['slug']}/team/leave")
    assert r.status_code == 403


def test_two_people_racing_for_the_last_seat(server):
    e = event_with_entries(1, max_team_size=2)
    racers = [person(f"seat{i}.{e['tag']}@example.org") for i in range(6)]
    with ThreadPoolExecutor(6) as pool:
        codes = list(pool.map(lambda t: client(t).post(f"/join/{e['teams'][0]['invite_code']}").status_code, racers))
    assert codes.count(200) == 1
    assert sql("SELECT count(*) AS n FROM team_members WHERE team_id = %s", (e["teams"][0]["id"],))[0]["n"] == 2


def test_cookie_writes_need_csrf(server):
    import httpx
    from conftest import BASE
    c = httpx.Client(base_url=BASE)
    c.post("/login", data={"email": "organizer@dogfood.local", "password": "dogfood", "next": "/"})
    r = c.post("/organize/sample-hack-2026/assign", data={})                   # a forged cross-site form post
    assert r.status_code == 403
    r = c.post("/organize/sample-hack-2026/assign", json={})
    assert r.status_code == 403 and r.json()["error"] == "csrf_failed"


def test_votes_are_not_in_the_organizer_readable_audit_log(server):
    e = event_with_entries(2)
    open_voting(e)
    v = person(f"priv.{e['tag']}@example.org")
    client(v).post(f"/api/v1/events/{e['slug']}/votes", json={"project_id": e["teams"][0]["project"]})
    log = client(ORGANIZER).get(f"/organize/{e['slug']}/audit").json()
    assert log["chain_ok"] and not [x for x in log["log"] if "vote" in x["action"]]
