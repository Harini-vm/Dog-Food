"""The assignment planner and the results engine, checked with small hand-made inputs and the fixture."""

from conftest import ORGANIZER
from dogfood.services.assign import plan
from dogfood.services.results import review_value

RUBRIC = [{"key": "functionality", "weight": 0.40, "min_score": 1, "max_score": 5},
          {"key": "quality", "weight": 0.35, "min_score": 1, "max_score": 5},
          {"key": "innovation", "weight": 0.25, "min_score": 1, "max_score": 5}]


def test_review_value_is_0_to_100():
    assert review_value({"functionality": 1, "quality": 1, "innovation": 1}, RUBRIC) == 0
    assert review_value({"functionality": 5, "quality": 5, "innovation": 5}, RUBRIC) == 100
    assert round(review_value({"functionality": 5, "quality": 1, "innovation": 1}, RUBRIC), 6) == 40
    assert review_value({"functionality": 5}, RUBRIC) is None                  # incomplete: not counted


def _projects(n, track=None):
    return [{"id": f"p{i}", "team_id": f"t{i}", "track_id": track} for i in range(n)]


def test_plan_is_balanced_and_deterministic():
    judges = [f"j{i}" for i in range(4)]
    new, short = plan(_projects(10), judges, {}, set(), [], 3, seed=7)
    assert not short and len(new) == 30 and len(set(new)) == 30
    loads = [sum(1 for j, _ in new if j == x) for x in judges]
    assert max(loads) - min(loads) <= 1
    assert plan(_projects(10), judges, {}, set(), [], 3, seed=7)[0] == new


def test_plan_never_breaks_a_conflict_and_reports_shortfalls():
    new, short = plan(_projects(2), ["j0", "j1"], {}, {("j0", "t0")}, [], 2, seed=1)
    assert ("j0", "p0") not in new
    assert short == [{"project": "p0", "has": 1, "needs": 2}]


def test_plan_hardest_first():
    # p0 is Beta-only and only j1 judges Beta; the planner must not spend j1 on the easy projects first.
    projects = [{"id": "p0", "team_id": "t0", "track_id": "beta"}] + \
               [{"id": f"p{i}", "team_id": f"t{i}", "track_id": None} for i in range(1, 4)]
    new, short = plan(projects, ["j0", "j1"], {"j0": {"alpha"}}, set(), [], 1, seed=3)
    assert ("j1", "p0") in new and not [s for s in short if s["project"] == "p0"]


def test_fixture_results(api):
    res = api(ORGANIZER).get("/organize/evt_01/results").json()
    ids = [r["project_id"] for r in res["ranking"]]
    assert "prj_41" not in ids and len(ids) == 40                              # duplicate never ranked
    assert [f["judge_id"] for f in res["flags"] if f["kind"] == "flat_judge"] == ["jdg_07"]
    top = res["ranking"][0]
    assert top["rank"] == 1 and 0 <= top["score"] <= 100


def test_results_preview_is_organizers_only(api):
    from conftest import JUDGE_A, PARTICIPANT
    assert api(JUDGE_A).get("/organize/evt_01/results").status_code == 403
    assert api(PARTICIPANT).get("/organize/evt_01/results").status_code == 403
    r = api().get("/organize/evt_01/results")                                 # anonymous browser: to the login page
    assert r.status_code == 303 and r.headers["location"].startswith("/login")
