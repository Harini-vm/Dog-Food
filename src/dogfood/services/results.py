"""Turning reviews into a ranking, and publishing it. JUDGING.md explains every step with numbers.

1. Each review becomes one number from 0 to 100: every criterion is rescaled to 0..1 by its own
   min/max, then combined with the rubric weights, then multiplied by 100.
2. Judges differ: one gives everybody 4s, another never goes above 3. We put every review on a
   common footing with a per-judge z-score, but shrunk toward the whole panel, because a judge with
   three reviews does not tell us much about their habits:
       judge mean  m' = (n*m + K*M) / (n + K)
       judge var   v' = (n*v + K*S^2) / (n + K)
       adjusted    a  = M + S * (x - m') / sqrt(v')
   M and S are the panel mean and standard deviation, n the judge's review count, K = 3.
   A judge with many reviews is corrected fully; a judge with one is barely corrected.
3. A project's score is the mean of its adjusted reviews, with one extra "average review" (C = 1)
   added, so a project with two lucky reviews does not outrank one with five solid ones.
4. Rank by that score. Equal to two decimals = shared rank; we do not invent a tie-breaker.

Only submitted projects count. Withdrawn and duplicate entries keep their reviews in the export but
never appear in results.
"""

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime, timezone

from .. import audit, db
from ..errors import bad, conflict

K = 3            # judge shrinkage, in reviews
C = 1            # project prior, in reviews
FLAT_SD = 2.0    # a judge whose reviews vary less than this (0-100 scale) is flagged
SPLIT = 40.0     # adjusted reviews of one project further apart than this are flagged
METHOD = f"shrunk-judge-z(K={K})+project-prior(C={C})"


def review_value(values: dict, rubric: list) -> float | None:
    num = den = 0.0
    for c in rubric:
        if c["key"] not in values:
            return None                                  # incomplete review: not counted
        w = float(c["weight"])
        num += w * (values[c["key"]] - c["min_score"]) / (c["max_score"] - c["min_score"])
        den += w
    return 100 * num / den if den else None


def _mean_var(xs):
    m = sum(xs) / len(xs)
    return m, sum((x - m) ** 2 for x in xs) / len(xs)


def adjust(reviews: list, k: float = K) -> tuple[float, float, dict]:
    """Pure function, no database: sets r["a"] (the adjusted value) on every review in place.
    Used by compute() and, unchanged, by tools/normalization_proof.py.
    k = 0 is a plain per-judge z-score; k -> infinity is no correction at all."""
    if not reviews:
        return 0.0, 0.0, {}
    M, var = _mean_var([r["x"] for r in reviews])
    S = math.sqrt(var)
    by_judge = defaultdict(list)
    for r in reviews:
        by_judge[r["judge"]].append(r)
    stats = {}
    for j, rs in by_judge.items():
        m, v = _mean_var([r["x"] for r in rs])
        n = len(rs)
        m2 = (n * m + k * M) / (n + k)
        sd2 = math.sqrt((n * v + k * var) / (n + k))
        for r in rs:
            r["a"] = M + S * (r["x"] - m2) / sd2 if sd2 > 1e-9 else M
        stats[j] = {"n": n, "mean": m, "sd": math.sqrt(v), "shrunk_mean": m2}
    return M, S, stats


def project_score(adjusted: list, M: float, c: float = C) -> float:
    """Mean of the adjusted reviews plus c 'average reviews' (the project prior)."""
    return (sum(adjusted) + c * M) / (len(adjusted) + c)


def compute(conn, event_id: str) -> dict:
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s", (event_id,))
    rubric = db.rows(conn, "SELECT * FROM criteria WHERE event_id = %s ORDER BY position", (event_id,))
    projects = {p["id"]: p for p in db.rows(conn, """
        SELECT p.id, p.title, p.track_id, t.name AS track, tm.name AS team FROM projects p
        JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks t ON t.id = p.track_id
        WHERE p.event_id = %s AND p.status = 'submitted'""", (event_id,))}
    raw = db.rows(conn, """
        SELECT s.judge_id, u.name AS judge, s.project_id, jsonb_object_agg(c.key, i.value) AS v
        FROM scores s JOIN users u ON u.id = s.judge_id JOIN score_items i ON i.score_id = s.id
        JOIN criteria c ON c.id = i.criterion_id WHERE s.event_id = %s
        GROUP BY s.id, s.judge_id, u.name, s.project_id""", (event_id,))
    reviews = []
    for r in raw:
        x = review_value(r["v"], rubric)
        if x is not None and r["project_id"] in projects:
            reviews.append({"judge": r["judge_id"], "judge_name": r["judge"], "project": r["project_id"],
                            "x": x, "flat_items": len(set(r["v"].values())) == 1})
    pending = db.rows(conn, """SELECT a.project_id, count(*) AS n FROM assignments a
                               JOIN projects p ON p.id = a.project_id AND p.status = 'submitted'
                               LEFT JOIN scores s ON s.assignment_id = a.id
                               WHERE a.event_id = %s AND s.id IS NULL GROUP BY a.project_id""", (event_id,))
    pending = {r["project_id"]: r["n"] for r in pending}

    judges, flags = {}, []
    M, S, stats = adjust(reviews)
    for j, st in stats.items():
        n, m, sd = st["n"], st["mean"], st["sd"]
        name = next(r["judge_name"] for r in reviews if r["judge"] == j)
        judges[j] = {"judge_id": j, "name": name, "reviews": n, "mean": round(m, 2), "sd": round(sd, 2),
                     "shift": round(M - st["shrunk_mean"], 2)}
        if n >= 3 and sd < FLAT_SD:
            judges[j]["flag"] = "flat"
            flags.append({"kind": "flat_judge", "judge_id": j, "name": name,
                          "detail": f"{n} reviews, spread {sd:.1f} points: barely tells projects apart"})
        elif n >= 3 and all(r["flat_items"] for r in reviews if r["judge"] == j):
            judges[j]["flag"] = "same_every_criterion"
            flags.append({"kind": "straight_line", "judge_id": j, "name": name,
                          "detail": "gives every criterion the same value in every review"})

    per_project = defaultdict(list)
    for r in reviews:
        per_project[r["project"]].append(r)
    rows = []
    for pid, p in projects.items():
        rs = per_project.get(pid, [])
        row = {"project_id": pid, "title": p["title"], "team": p["team"], "track": p["track"],
               "track_id": p["track_id"], "reviews": len(rs), "pending": pending.get(pid, 0)}
        if rs:
            row["raw"] = round(sum(r["x"] for r in rs) / len(rs), 2)
            row["score"] = round(project_score([r["a"] for r in rs], M), 2)
            spread = max(r["a"] for r in rs) - min(r["a"] for r in rs)
            if len(rs) > 1 and spread > SPLIT:
                flags.append({"kind": "split_panel", "project_id": pid, "title": p["title"],
                              "detail": f"judges disagree by {spread:.0f} points"})
        if len(rs) < ev["reviews_per_project"]:
            flags.append({"kind": "under_reviewed", "project_id": pid, "title": p["title"],
                          "detail": f"{len(rs)} of {ev['reviews_per_project']} reviews"
                                    + (f", {pending[pid]} pending" if pid in pending else "")})
        rows.append(row)

    ranked = sorted([r for r in rows if "score" in r], key=lambda r: (-r["score"], r["title"].lower()))
    _rank(ranked, "rank")
    raw_order = sorted(ranked, key=lambda r: (-r["raw"], r["title"].lower()))
    _rank(raw_order, "raw_rank")
    for r in ranked:
        r["moved"] = r["raw_rank"] - r["rank"]           # +3 = normalization moved it up three places
    tracks = defaultdict(list)
    for r in ranked:
        tracks[r["track"] or "No track"].append(r)
    for rs in tracks.values():
        _rank(rs, "track_rank")
    unranked = [r for r in rows if "score" not in r]
    return {"event_id": event_id, "method": METHOD, "panel": {"mean": round(M, 2), "sd": round(S, 2),
            "reviews": len(reviews)}, "ranking": ranked, "unranked": unranked,
            "judges": sorted(judges.values(), key=lambda j: j["name"]), "flags": flags,
            "complete": not pending and not any(f["kind"] == "under_reviewed" for f in flags)}


def _rank(rows, field):
    prev, rank = None, 0
    for i, r in enumerate(rows, 1):
        key = r["score"] if field != "raw_rank" else r["raw"]
        if key != prev:
            rank, prev = i, key
        r[field] = rank


def public_body(res: dict) -> dict:
    """What the world sees. No judges, no individual reviews, no flags."""
    keep = ("rank", "track_rank", "project_id", "title", "team", "track", "score", "reviews")
    return {"method": res["method"], "ranking": [{k: r[k] for k in keep} for r in res["ranking"]],
            "unranked": [{"project_id": r["project_id"], "title": r["title"], "team": r["team"]}
                         for r in res["unranked"]]}


def publish(conn, user, ev, force: bool = False) -> dict:
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s FOR UPDATE", (ev["id"],))   # blocks concurrent score saves
    if ev["results_published_at"]:
        raise conflict("results are already published")
    if datetime.now(timezone.utc) < ev["closes_at"]:
        raise conflict("submissions are still open; results can be published after they close", "still_open")
    res = compute(conn, ev["id"])
    if not res["ranking"]:
        raise bad("no reviewed projects to rank yet")
    if not res["complete"] and not force:
        raise conflict("some projects are missing reviews; confirm to publish anyway", "incomplete")
    body = {**public_body(res), "event": {"id": ev["id"], "name": ev["name"], "slug": ev["slug"]},
            "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    version = (db.val(conn, "SELECT max(version) FROM results WHERE event_id = %s", (ev["id"],)) or 0) + 1
    h = hashlib.sha256(canonical.encode()).hexdigest()
    conn.execute("INSERT INTO results (event_id, version, method, body, body_hash, published_by) "
                 "VALUES (%s, %s, %s, %s, %s, %s)", (ev["id"], version, METHOD, db.jsonb(body), h, user.id))
    conn.execute("UPDATE events SET results_published_at = now() WHERE id = %s", (ev["id"],))
    audit.log(conn, user, "results.publish", ev["id"], f"v{version}",
              {"hash": h, "forced": force and not res["complete"], "ranked": len(body["ranking"])})
    from . import webhooks
    webhooks.emit(conn, ev["id"], "results.published", {"version": version, "sha256": h,
                                                          "top": body["ranking"][:3]})
    return {"version": version, "hash": h}


def retract(conn, user, ev, reason: str, force: bool = False) -> None:
    if not reason.strip():
        raise bad("say why the results are being retracted; it goes in the audit log")
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s FOR UPDATE", (ev["id"],))
    if not ev["results_published_at"]:
        raise conflict("results are not published")
    if db.one(conn, "SELECT 1 FROM certificates WHERE event_id = %s", (ev["id"],)) and not (force and user.is_admin):
        raise conflict("signed certificates were issued from these results; only an admin can retract now",
                       "certificates_issued")
    conn.execute("UPDATE results SET retracted_at = now() WHERE event_id = %s AND retracted_at IS NULL", (ev["id"],))
    conn.execute("UPDATE events SET results_published_at = NULL WHERE id = %s", (ev["id"],))
    audit.log(conn, user, "results.retract", ev["id"], ev["id"], {"reason": reason.strip()[:500], "forced": force})
    from . import webhooks
    webhooks.emit(conn, ev["id"], "results.retracted", {"reason": reason.strip()[:500]})


def published(conn, event_id: str):
    return db.one(conn, """SELECT * FROM results WHERE event_id = %s AND retracted_at IS NULL
                           ORDER BY version DESC LIMIT 1""", (event_id,))
