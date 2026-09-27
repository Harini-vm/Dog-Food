"""T3 voting: windows, budgets under concurrency, ballots, self-votes, sealed tallies, email links."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import ADMIN, ORGANIZER
from helpers import client, close_voting, event_with_entries, open_voting, person, sql


@pytest.fixture(scope="module")
def ev(server):
    e = event_with_entries(6)
    open_voting(e, votes=3)
    return e


@pytest.fixture(autouse=True)
def _fresh_limits():
    from dogfood import ratelimit
    ratelimit.reset()


def _voter(ev, n):
    return person(f"voter{n}.{ev['tag']}@example.org")


def test_voting_window_is_server_side(server):
    e = event_with_entries(1)
    v = person(f"early.{e['tag']}@example.org")
    r = client(v).post(f"/api/v1/events/{e['slug']}/votes", json={"project_id": e["teams"][0]["project"]})
    assert r.status_code == 403 and r.json()["error"] == "voting_closed"          # never opened
    open_voting(e)
    close_voting(e)
    r = client(v).post(f"/api/v1/events/{e['slug']}/votes", json={"project_id": e["teams"][0]["project"]})
    assert r.status_code == 403 and r.json()["error"] == "voting_closed"          # closed: opens <= t < closes


def test_same_vote_ten_times_at_once_is_one_vote(ev):
    v = _voter(ev, "dup")
    pid = ev["teams"][0]["project"]
    with ThreadPoolExecutor(10) as pool:
        codes = list(pool.map(lambda _: client(v).post(f"/api/v1/events/{ev['slug']}/votes",
                                                      json={"project_id": pid}).status_code, range(10)))
    assert set(codes) == {200}
    assert sql("SELECT count(*) AS n FROM votes WHERE project_id = %s", (pid,))[0]["n"] == 1


def test_budget_holds_under_concurrency(ev):
    from dogfood import ratelimit
    old = ratelimit.RULES["vote"]
    ratelimit.RULES["vote"] = (1000, 60)
    try:
        v = _voter(ev, "race")
        pids = [t["project"] for t in ev["teams"]]
        with ThreadPoolExecutor(20) as pool:
            codes = list(pool.map(lambda i: client(v).post(f"/api/v1/events/{ev['slug']}/votes",
                                                          json={"project_id": pids[i % 6]}).status_code, range(20)))
    finally:
        ratelimit.RULES["vote"] = old
    uid = sql("SELECT id FROM users WHERE email = %s", (f"voterrace.{ev['tag']}@example.org",))[0]["id"]
    stored = sql("SELECT count(*) AS n FROM votes v JOIN voters r ON r.id = v.voter_id WHERE r.user_id = %s", (uid,))
    assert stored[0]["n"] == 3 and 409 in codes                                    # never an overspend


def test_ballot_is_a_stable_personal_permutation(ev):
    a, b = _voter(ev, "ba"), _voter(ev, "bb")
    first = [p["id"] for p in client(a).get(f"/api/v1/events/{ev['slug']}/ballot").json()["projects"]]
    again = [p["id"] for p in client(a).get(f"/api/v1/events/{ev['slug']}/ballot").json()["projects"]]
    other = [p["id"] for p in client(b).get(f"/api/v1/events/{ev['slug']}/ballot").json()["projects"]]
    everyone = {t["project"] for t in ev["teams"]}
    assert first == again and set(first) == everyone and len(first) == len(everyone)
    assert sorted(first) == sorted(other)


def test_no_votes_for_own_team(ev):
    cap = ev["teams"][1]
    r = client(cap["token"]).post(f"/api/v1/events/{ev['slug']}/votes", json={"project_id": cap["project"]})
    assert r.status_code == 403 and r.json()["error"] == "own_team"


def test_other_events_projects_are_not_on_the_ballot(ev):
    v = _voter(ev, "cross")
    r = client(v).post(f"/api/v1/events/{ev['slug']}/votes", json={"project_id": "prj_01"})
    assert r.status_code == 404


def test_tallies_sealed_for_everyone_until_close(server):
    e = event_with_entries(2)
    open_voting(e)
    v = person(f"t.{e['tag']}@example.org")
    assert client(v).post(f"/api/v1/events/{e['slug']}/votes", json={"project_id": e["teams"][0]["project"]}).status_code == 200
    for tok in (None, v, ORGANIZER, ADMIN):
        r = client(tok).get(f"/api/v1/events/{e['slug']}/tally")
        assert r.status_code == 403 and r.json()["error"] == "tallies_sealed"
    page = client(ORGANIZER).get(f"/organize/{e['slug']}")
    assert page.status_code == 200 and "votes" not in str(page.json())            # dashboard JSON: no counts
    close_voting(e)
    tally = client().get(f"/api/v1/events/{e['slug']}/tally").json()["tally"]
    assert {t["project_id"]: t["votes"] for t in tally}[e["teams"][0]["project"]] == 1


def test_withdrawn_project_gives_the_vote_back(server):
    e = event_with_entries(3)
    open_voting(e, votes=1)
    v = person(f"w.{e['tag']}@example.org")
    p0, p1 = e["teams"][0]["project"], e["teams"][1]["project"]
    assert client(v).post(f"/api/v1/events/{e['slug']}/votes", json={"project_id": p0}).json()["left"] == 0
    client(ORGANIZER).post(f"/organize/{e['slug']}/projects/{p0}/status", json={"status": "withdrawn", "reason": "x"})
    ballot = client(v).get(f"/api/v1/events/{e['slug']}/ballot").json()
    assert ballot["votes_left"] == 1 and p0 not in [p["id"] for p in ballot["projects"]]
    assert client(v).post(f"/api/v1/events/{e['slug']}/votes", json={"project_id": p1}).status_code == 200
    assert client(v).post(f"/api/v1/events/{e['slug']}/votes", json={"project_id": p0}).status_code == 404


def test_email_links(ev):
    base = f"/events/{ev['slug']}/vote/link"
    one = client().post(base, json={"email": f"  Guest.{ev['tag']}@Example.COM "}).json()["link"]
    two = client().post(base, json={"email": f"guest.{ev['tag']}@example.com"}).json()["link"]
    path1, path2 = "/" + one.split("/", 3)[3], "/" + two.split("/", 3)[3]
    b1 = client()
    assert b1.get(path1).status_code == 200                                       # a GET never uses the link
    assert b1.post(path1).status_code == 303
    assert client().post(path1).status_code == 404                                # used once
    assert client().post(path2[:-1] + ("A" if path2[-1] != "A" else "B")).status_code == 404   # altered
    b2 = client()
    assert b2.post(path2).status_code == 303
    pid = ev["teams"][2]["project"]
    page = b1.get(f"/events/{ev['slug']}/vote").text
    import re
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    assert b1.post(f"/events/{ev['slug']}/vote", data={"csrf": csrf, "project_id": pid, "action": "vote"}).status_code == 303
    page2 = b2.get(f"/events/{ev['slug']}/vote").text                              # same address, same voter
    assert "your vote" in page2 and "2 of 3" in page2
    assert b1.post(f"/events/{ev['slug']}/vote", data={"csrf": "wrong", "project_id": pid}).status_code == 403
    assert client().post(base, json={"email": "not-an-email"}).status_code == 400


def test_email_voter_who_is_on_the_team_is_refused(ev):
    cap = ev["teams"][3]
    link = client().post(f"/events/{ev['slug']}/vote/link", json={"email": cap["email"].upper()}).json()["link"]
    b = client()
    b.post("/" + link.split("/", 3)[3])
    import re
    csrf = re.search(r'name="csrf" value="([^"]+)"', b.get(f"/events/{ev['slug']}/vote").text).group(1)
    r = b.post(f"/events/{ev['slug']}/vote", data={"csrf": csrf, "project_id": cap["project"]},
               headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 403
