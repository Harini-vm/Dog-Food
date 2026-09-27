"""Pairwise mode: the Bradley-Terry model, which pairs are generated, privacy, and locking."""

import random

import pytest

from conftest import ORGANIZER
from helpers import client, event_with_entries, sql, token_for
from dogfood.services.pairwise import bradley_terry, kendall_tau


def test_bradley_terry_recovers_a_known_order():
    rng = random.Random(1)
    truth = {f"p{i}": 2.0 ** (i / 3) for i in range(12)}          # p11 strongest
    outcomes = []
    for _ in range(400):
        a, b = rng.sample(sorted(truth), 2)
        outcomes.append((a, b, "a" if rng.random() < truth[a] / (truth[a] + truth[b]) else "b"))
    p = bradley_terry(truth, outcomes)
    fitted = sorted(p, key=lambda i: -p[i])
    assert kendall_tau(sorted(truth, key=lambda i: -truth[i]), fitted) > 0.8


def test_undefeated_entry_gets_a_finite_strength_and_ties_count_half():
    p = bradley_terry(["a", "b", "c"], [("a", "b", "a"), ("a", "c", "a"), ("b", "c", "tie")])
    assert p["a"] > p["b"] and 0 < p["a"] < 100                      # the prior keeps it finite
    assert abs(p["b"] - p["c"]) < 1e-6                               # a draw between equals stays equal


def test_kendall_tau_extremes():
    assert kendall_tau(list("abcd"), list("abcd")) == 1
    assert kendall_tau(list("abcd"), list("dcba")) == -1
    assert kendall_tau(["a"], ["a"]) is None


@pytest.fixture(scope="module")
def ev(server):
    e = event_with_entries(5)
    judges = []
    for i in range(2):
        email = f"pw{i}.{e['tag']}@example.org"
        client(ORGANIZER).post(f"/organize/{e['slug']}/people", json={"email": email})
        judges.append(token_for(email))
    # judge 0 is also a member of team 0: a conflict of interest
    uid = sql("SELECT id FROM users WHERE email = %s", (f"pw0.{e['tag']}@example.org",))[0]["id"]
    sql("DELETE FROM memberships WHERE event_id = %s AND user_id = %s AND role = 'judge'", (e["id"], uid))
    sql("INSERT INTO team_members VALUES (%s, %s, %s, false)", (e["teams"][0]["id"], e["id"], uid))
    sql("INSERT INTO memberships VALUES (%s, %s, 'judge')", (e["id"], uid))
    sql("INSERT INTO conflicts VALUES (%s, %s, %s, 'judge is on this team')", (e["id"], uid, e["teams"][0]["id"]))
    r = client(ORGANIZER).post(f"/organize/{e['slug']}/pairwise", json={"per_judge": 3})
    assert r.status_code == 200 and r.json()["created"] == 6, r.text
    return {**e, "judges": judges, "conflicted": uid}


def test_generated_pairs_follow_the_rules(ev):
    rows = sql("SELECT judge_id, project_a, project_b FROM comparisons WHERE event_id = %s", (ev["id"],))
    own = ev["teams"][0]["project"]
    assert not [r for r in rows if r["judge_id"] == ev["conflicted"] and own in (r["project_a"], r["project_b"])]
    assert len({(r["judge_id"], r["project_a"], r["project_b"]) for r in rows}) == len(rows)
    again = client(ORGANIZER).post(f"/organize/{ev['slug']}/pairwise", json={"per_judge": 3}).json()
    assert again["created"] == 0                                      # tops up only


def test_judges_answer_only_their_own(ev):
    mine = client(ev["judges"][1]).get("/api/v1/judge/pairs").json()["pairs"]
    theirs = client(ev["judges"][0]).get("/api/v1/judge/pairs").json()["pairs"]
    assert mine and theirs and not {p["id"] for p in mine} & {p["id"] for p in theirs}
    r = client(ev["judges"][1]).put(f"/api/v1/pairs/{theirs[0]['id']}", json={"winner": "tie"})
    assert r.status_code == 403
    r = client(ev["judges"][1]).put(f"/api/v1/pairs/{mine[0]['id']}", json={"winner": "prj_not_in_pair"})
    assert r.status_code == 400
    for tok in ev["judges"]:
        for p in client(tok).get("/api/v1/judge/pairs").json()["pairs"]:
            if p["event_id"] == ev["id"]:
                assert client(tok).put(f"/api/v1/pairs/{p['id']}", json={"winner": p["project_a"]}).status_code == 200
    audit = [r["detail"] for r in sql("SELECT detail FROM audit_log WHERE action = 'pairwise.decide' AND event_id = %s",
                                      (ev["id"],))]
    assert audit and all(d == {} for d in audit)                      # the log never says who won


def test_preview_shows_the_cross_check(ev):
    res = client(ORGANIZER).get(f"/organize/{ev['slug']}/results").json()
    pw = res["pairwise"]
    assert pw["decided"] == 6 and pw["scores"]
    assert pw["agree"] + pw["against"] == 0                  # no rubric scores yet, so nothing to agree with
    assert pw["enough"] is False and pw["tau"] is None      # 6 answers over 5 entries: too few for a rank correlation


def test_comparisons_lock_with_results(ev):
    import psycopg
    judge_tok = client(ORGANIZER)
    a = [x for x in client(ev["judges"][1]).get("/api/v1/judge/pairs").json()["pairs"] if x["event_id"] == ev["id"]][0]
    # give every entry one review so results can be published
    j = token_for(f"pw1.{ev['tag']}@example.org")
    judge_tok.post(f"/organize/{ev['slug']}/assign")
    for asg in client(j).get("/api/v1/judge/assignments").json()["assignments"]:
        if asg["event_id"] == ev["id"]:
            client(j).put(f"/api/v1/assignments/{asg['id']}/score",
                          json={"criteria": {"functionality": 3, "quality": 3, "innovation": 3}})
    sql("UPDATE events SET closes_at = now() - interval '1 minute' WHERE id = %s", (ev["id"],))
    assert judge_tok.post(f"/organize/{ev['slug']}/publish", json={"force": True}).status_code == 201
    r = client(ev["judges"][1]).put(f"/api/v1/pairs/{a['id']}", json={"winner": "tie"})
    assert r.status_code == 403 and r.json()["error"] == "judging_closed"
    with pytest.raises(psycopg.Error) as e:
        sql("UPDATE comparisons SET winner = 'tie' WHERE id = %s", (a["id"],))
    assert e.value.sqlstate == "DF004"
