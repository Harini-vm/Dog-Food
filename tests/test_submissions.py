"""The deadline, the one-entry-per-team rule and the fixture's duplicate submission."""

import psycopg
import pytest

from conftest import PARTICIPANT


def test_checker_probe_refused_with_reason(api):
    r = api(PARTICIPANT).post("/projects/new", json={"title": "late", "summary": "probe"})
    assert r.status_code == 403 and r.json()["error"] == "submissions_closed"


def test_database_refuses_late_edits_even_from_raw_sql(sql):
    with pytest.raises(psycopg.Error) as e:
        sql("UPDATE projects SET title = 'sneaky' WHERE id = 'prj_01'")
    assert e.value.sqlstate == "DF001"


def test_moderation_after_deadline_is_allowed(sql):
    sql("UPDATE projects SET status = 'withdrawn' WHERE id = 'prj_02'")
    sql("SET LOCAL dogfood.importing = 'on'; UPDATE projects SET status = 'submitted' WHERE id = 'prj_02'")


def test_fixture_duplicate_is_resolved(sql, api):
    row = sql("SELECT status, duplicate_of FROM projects WHERE id = 'prj_41'")[0]
    assert row == {"status": "duplicate", "duplicate_of": "prj_07"}
    assert api().get("/projects/prj_41").status_code == 404
    with pytest.raises(psycopg.Error):   # a second live entry for a team cannot be stored
        sql("UPDATE projects SET status = 'submitted', duplicate_of = NULL WHERE id = 'prj_41'")


def test_gallery_hides_duplicates_and_searches(api):
    html = api().get("/projects?event=sample-hack-2026").text
    assert html.count("Dry Harbour") == 1
    assert "Glass Signal" in api().get("/projects?q=glass").text
    assert "Glass Signal" not in api().get("/projects?q=zzzz").text
