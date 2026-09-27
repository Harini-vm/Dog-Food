"""Who reviews what. A greedy planner that is easy to explain and to check by hand.

Goal: every submitted project gets `reviews_per_project` reviews, spread evenly over the judges.

Rules (hard):
  * a judge never reviews a team they are on (conflicts table)
  * a judge with tracks only reviews projects in those tracks (no tracks = any track)
  * a judge reviews a project at most once (UNIQUE (judge_id, project_id) in the database)
Order:
  * hardest project first: the one with the fewest eligible judges, so easy projects cannot use up
    the only judges a hard one could have had
  * for each seat, the eligible judge with the lowest load; ties broken by a seeded shuffle, so the
    same event + seed always gives the same plan (re-runnable, and explainable in a dispute)
Existing assignments are kept and counted, so running the planner again only fills gaps. That is
what makes it safe after late judges join or a judge is removed.
"""

import random
from collections import defaultdict

from .. import audit, db
from ..errors import bad, conflict
from ..security import new_id


def _inputs(conn, event_id):
    projects = db.rows(conn, """SELECT p.id, p.team_id, p.track_id FROM projects p
                                WHERE p.event_id = %s AND p.status = 'submitted' ORDER BY p.id""", (event_id,))
    judges = [r["user_id"] for r in db.rows(conn, """SELECT user_id FROM memberships WHERE event_id = %s
                                                     AND role = 'judge' ORDER BY user_id""", (event_id,))]
    tracks = defaultdict(set)
    for r in db.rows(conn, "SELECT user_id, track_id FROM judge_tracks WHERE event_id = %s", (event_id,)):
        tracks[r["user_id"]].add(r["track_id"])
    blocked = {(r["judge_id"], r["team_id"]) for r in db.rows(
        conn, "SELECT judge_id, team_id FROM conflicts WHERE event_id = %s", (event_id,))}
    existing = db.rows(conn, "SELECT judge_id, project_id FROM assignments WHERE event_id = %s", (event_id,))
    return projects, judges, tracks, blocked, existing


def plan(projects, judges, judge_tracks, blocked, existing, per_project: int, seed: int):
    """Pure function (no database), so tests can check it directly. Returns (new pairs, shortfalls)."""
    rng = random.Random(seed)
    order = judges[:]
    rng.shuffle(order)
    tiebreak = {j: i for i, j in enumerate(order)}
    load = defaultdict(int)
    have = defaultdict(set)
    live = {p["id"] for p in projects}
    for e in existing:
        load[e["judge_id"]] += 1                       # all of a judge's work counts toward their load
        if e["project_id"] in live:
            have[e["project_id"]].add(e["judge_id"])

    def eligible(p):
        return [j for j in judges
                if (j, p["team_id"]) not in blocked
                and (not judge_tracks.get(j) or p["track_id"] in judge_tracks[j])
                and j not in have[p["id"]]]

    new, short = [], []
    for p in sorted(projects, key=lambda p: (len(eligible(p)), p["id"])):
        need = per_project - len(have[p["id"]])
        pool = eligible(p)
        for _ in range(max(need, 0)):
            if not pool:
                break
            j = min(pool, key=lambda j: (load[j], tiebreak[j]))
            pool.remove(j)
            load[j] += 1
            have[p["id"]].add(j)
            new.append((j, p["id"]))
        if len(have[p["id"]]) < per_project:
            short.append({"project": p["id"], "has": len(have[p["id"]]), "needs": per_project})
    return new, short


def run(conn, user, ev) -> dict:
    if ev["results_published_at"]:
        raise conflict("results are published; judging is over")
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("assign:" + ev["id"],))  # one planner at a time
    projects, judges, tracks, blocked, existing = _inputs(conn, ev["id"])
    if not judges:
        raise bad("invite at least one judge first", "no_judges")
    new, short = plan(projects, judges, tracks, blocked, existing, ev["reviews_per_project"], ev["assignment_seed"])
    batch = new_id("batch")
    for j, p in new:
        conn.execute("INSERT INTO assignments (id, event_id, judge_id, project_id, batch) VALUES (%s,%s,%s,%s,%s)",
                     (new_id("asg"), ev["id"], j, p, batch))
    report = {"batch": batch, "created": len(new), "shortfalls": short, "judges": len(judges),
              "projects": len(projects)}
    audit.log(conn, user, "assign.run", ev["id"], batch, {"created": len(new), "shortfalls": len(short)})
    return report


def assign_one(conn, user, ev, judge_id: str, project_id: str) -> str:
    p = db.one(conn, "SELECT * FROM projects WHERE id = %s AND event_id = %s", (project_id, ev["id"]))
    if p is None or p["status"] != "submitted":
        raise bad("only submitted projects of this event can be assigned")
    if not db.one(conn, "SELECT 1 FROM memberships WHERE event_id=%s AND user_id=%s AND role='judge'",
                  (ev["id"], judge_id)):
        raise bad("that person is not a judge of this event")
    if db.one(conn, "SELECT 1 FROM conflicts WHERE judge_id = %s AND team_id = %s", (judge_id, p["team_id"])):
        raise conflict("that judge has a conflict of interest with this team", "conflict_of_interest")
    if db.one(conn, "SELECT 1 FROM assignments WHERE judge_id = %s AND project_id = %s", (judge_id, project_id)):
        raise conflict("already assigned")
    aid = new_id("asg")
    conn.execute("INSERT INTO assignments (id, event_id, judge_id, project_id, batch) VALUES (%s,%s,%s,%s,'manual')",
                 (aid, ev["id"], judge_id, project_id))
    audit.log(conn, user, "assign.one", ev["id"], aid, {"judge": judge_id, "project": project_id})
    return aid


def unassign(conn, user, ev, assignment_id: str) -> None:
    a = db.one(conn, "SELECT * FROM assignments WHERE id = %s AND event_id = %s FOR UPDATE", (assignment_id, ev["id"]))
    if a is None:
        raise bad("no such assignment in this event")
    if db.one(conn, "SELECT 1 FROM scores WHERE assignment_id = %s", (assignment_id,)):
        raise conflict("this review is already scored; it cannot be unassigned", "already_scored")
    conn.execute("DELETE FROM assignments WHERE id = %s", (assignment_id,))
    audit.log(conn, user, "assign.remove", ev["id"], assignment_id)
