"""Score privacy, attacked from every route. Every refusal must come from the server."""

import pytest

from conftest import ADMIN, JUDGE_A, JUDGE_B, ORGANIZER, PARTICIPANT

A = "jdg_24"   # judge_a in .dogfood.toml


def test_judge_reads_only_own_scores(api):
    body = api(JUDGE_A).get("/api/v1/judge/scores").json()
    assert body["scores"] and {s["judge_id"] for s in body["scores"]} == {A}


@pytest.mark.parametrize("url", [f"/api/v1/judges/{A}/scores", f"/api/v1/judge/scores?judge={A}",
                                 "/api/v1/judges/no-such-judge/scores"])
def test_peer_judge_refused(api, url):
    assert api(JUDGE_B).get(url).status_code == 403


@pytest.mark.parametrize("url", ["/api/v1/judge/scores", f"/api/v1/judges/{A}/scores", "/api/v1/judge/assignments",
                                 "/api/v1/export/scores.csv"])
def test_participant_refused(api, url):
    assert api(PARTICIPANT).get(url).status_code == 403


@pytest.mark.parametrize("url", ["/api/v1/judge/scores", f"/api/v1/judges/{A}/scores", "/api/v1/export/scores.csv"])
def test_anonymous_401(api, url):
    assert api().get(url).status_code == 401


def test_bad_token_is_401_even_on_public_pages(api):
    assert api("forged").get("/projects").status_code == 401


def test_judge_cannot_write_a_peers_assignment(api, sql):
    aid = sql("SELECT id FROM assignments WHERE judge_id = %s LIMIT 1", (A,))[0]["id"]
    r = api(JUDGE_B).put(f"/api/v1/assignments/{aid}/score",
                         json={"criteria": {"functionality": 5, "quality": 5, "innovation": 5}})
    assert r.status_code == 403


def test_organizer_and_admin_can_read(api):
    assert api(ORGANIZER).get(f"/api/v1/judges/{A}/scores").json()["scores"]
    assert api(ADMIN).get(f"/api/v1/judges/{A}/scores").status_code == 200


def test_csv_export(api):
    r = api(ORGANIZER).get("/api/v1/export/scores.csv?event=evt_01")
    lines = r.text.splitlines()
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert lines[0].startswith("event_id,project_id") and len(lines) == 127   # header + 126 fixture reviews
