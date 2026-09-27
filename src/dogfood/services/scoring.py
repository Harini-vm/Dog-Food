"""Scores: reading them (after authorization), saving them, and exporting them."""

import csv
import io
from datetime import datetime, timezone

from .. import audit, db
from ..errors import bad, forbidden
from ..security import new_id


def criteria(conn, event_id):
    return db.rows(conn, "SELECT * FROM criteria WHERE event_id = %s ORDER BY position", (event_id,))


def weighted(values: dict, weights: dict) -> float:
    """sum(w * v) / sum(w): a review as one number on the rubric's own scale."""
    num = sum(weights[k] * v for k, v in values.items() if k in weights)
    den = sum(weights[k] for k in values if k in weights)
    return num / den if den else 0.0


def list_scores(conn, event_ids: list[str], judge_id: str | None = None):
    """Callers must already have authorized event_ids (and judge_id) with authz.score_scope."""
    return db.rows(conn, """
        SELECT s.id, s.event_id, s.judge_id, u.name AS judge, s.project_id, p.title, p.status, s.comment, s.updated_at,
               coalesce(jsonb_object_agg(c.key, i.value) FILTER (WHERE c.key IS NOT NULL), '{}') AS criteria
        FROM scores s JOIN users u ON u.id = s.judge_id JOIN projects p ON p.id = s.project_id
        LEFT JOIN score_items i ON i.score_id = s.id LEFT JOIN criteria c ON c.id = i.criterion_id
        WHERE s.event_id = ANY(%s) AND (%s::text IS NULL OR s.judge_id = %s)
        GROUP BY s.id, u.name, p.title, p.status ORDER BY s.event_id, u.name, p.title""",
                   (event_ids, judge_id, judge_id))


def judging_open(ev) -> bool:
    if ev["results_published_at"]:
        return False
    return ev["judging_closes_at"] is None or datetime.now(timezone.utc) < ev["judging_closes_at"]


def save(conn, user, assignment_id: str, values: dict, comment: str) -> str:
    a = db.one(conn, "SELECT * FROM assignments WHERE id = %s FOR UPDATE", (assignment_id,))
    if a is None or a["judge_id"] != user.id:              # same answer for "missing" and "not yours"
        raise forbidden("this is not your assignment")
    if not db.one(conn, "SELECT 1 FROM memberships WHERE event_id=%s AND user_id=%s AND role='judge'",
                  (a["event_id"], user.id)):
        raise forbidden("you are no longer a judge of this event", "not_a_judge")
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s FOR SHARE", (a["event_id"],))
    if not judging_open(ev):
        raise forbidden("judging is closed", "judging_closed")
    rubric = criteria(conn, a["event_id"])
    for c in rubric:
        v = values.get(c["key"])
        if isinstance(v, bool) or not isinstance(v, int) or not c["min_score"] <= v <= c["max_score"]:
            raise bad(f"{c['name']} needs a whole number from {c['min_score']} to {c['max_score']}")
    if len(comment) > 5000:
        raise bad("comment is too long (5000 characters)")
    sid = db.val(conn, "SELECT id FROM scores WHERE assignment_id = %s", (assignment_id,))
    if sid:
        conn.execute("UPDATE scores SET comment = %s, updated_at = now() WHERE id = %s", (comment, sid))
        conn.execute("DELETE FROM score_items WHERE score_id = %s", (sid,))
    else:
        sid = new_id("scr")
        conn.execute("INSERT INTO scores (id, assignment_id, event_id, judge_id, project_id, comment) "
                     "VALUES (%s,%s,%s,%s,%s,%s)", (sid, assignment_id, a["event_id"], user.id, a["project_id"], comment))
    for c in rubric:
        conn.execute("INSERT INTO score_items VALUES (%s, %s, %s)", (sid, c["id"], values[c["key"]]))
    # The audit line says a score changed, never the values: organizers read the log during judging.
    audit.log(conn, user, "score.save", a["event_id"], a["project_id"])
    return sid


def csv_for(conn, event_ids: list[str]) -> str:
    keys, weights = [], {}
    for eid in event_ids:
        for c in criteria(conn, eid):
            weights.setdefault(eid, {})[c["key"]] = float(c["weight"])
            if c["key"] not in keys:
                keys.append(c["key"])
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(["event_id", "project_id", "project_title", "project_status", "judge_id", "judge_name", *keys,
                "weighted_total", "comment", "updated_at"])
    for r in list_scores(conn, event_ids):
        w.writerow([r["event_id"], r["project_id"], r["title"], r["status"], r["judge_id"], r["judge"],
                    *[r["criteria"].get(k, "") for k in keys],
                    round(weighted(r["criteria"], weights.get(r["event_id"], {})), 4), r["comment"],
                    r["updated_at"].isoformat()])
    return out.getvalue()
