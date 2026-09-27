"""REST API v1. Bearer tokens; JSON errors {"error", "message"}."""

from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import audit, authz, db
from ..errors import forbidden
from ..auth import user
from ..services import scoring

router = APIRouter(prefix="/api/v1")


class ScoreIn(BaseModel):
    criteria: dict[str, int] = Field(..., examples=[{"functionality": 4, "quality": 3, "innovation": 5}])
    comment: str = ""


def _out(r):
    return {"id": r["id"], "event_id": r["event_id"], "judge_id": r["judge_id"], "project_id": r["project_id"],
            "project_title": r["title"], "criteria": r["criteria"], "comment": r["comment"],
            "updated_at": r["updated_at"].isoformat()}


@router.get("/me", tags=["identity"])
def me(request: Request):
    u = authz.need_login(user(request))
    with db.tx() as conn:
        rs = db.rows(conn, "SELECT event_id, array_agg(role) AS roles FROM memberships WHERE user_id = %s "
                           "GROUP BY event_id", (u.id,))
    return {"id": u.id, "email": u.email, "name": u.name, "is_admin": u.is_admin,
            "roles": {r["event_id"]: sorted(r["roles"]) for r in rs}}


@router.get("/judge/scores", tags=["judging"], summary="My own scores (judges only)")
def my_scores(request: Request, judge: str | None = None):
    """`?judge=<id>` for anyone but yourself is refused unless you organize their event."""
    u = user(request)
    with db.tx() as conn:
        target = judge or authz.need_login(u).id
        events = authz.score_scope(conn, u, target)
        return {"judge_id": target, "scores": [_out(r) for r in scoring.list_scores(conn, events, target)]}


@router.get("/judges/{judge_id}/scores", tags=["judging"], summary="One judge's scores (that judge or their organizer)")
def judge_scores(request: Request, judge_id: str):
    with db.tx() as conn:
        events = authz.score_scope(conn, user(request), judge_id)
        return {"judge_id": judge_id, "scores": [_out(r) for r in scoring.list_scores(conn, events, judge_id)]}


@router.get("/judge/assignments", tags=["judging"], summary="My assignments")
def my_assignments(request: Request):
    u = authz.need_login(user(request))
    with db.tx() as conn:
        rows = db.rows(conn, """SELECT a.id, a.event_id, a.project_id, p.title, s.id IS NOT NULL AS scored
                                FROM assignments a JOIN projects p ON p.id = a.project_id
                                JOIN memberships m ON m.event_id = a.event_id AND m.user_id = a.judge_id AND m.role = 'judge'
                                LEFT JOIN scores s ON s.assignment_id = a.id
                                WHERE a.judge_id = %s ORDER BY a.event_id, p.title""", (u.id,))
        if not rows and not db.one(conn, "SELECT 1 FROM memberships WHERE user_id = %s AND role = 'judge'", (u.id,)):
            authz.score_scope(conn, u, u.id)                 # raises: not a judge
    return {"assignments": rows}


@router.put("/assignments/{assignment_id}/score", tags=["judging"], summary="Create or replace my score")
def put_score(request: Request, assignment_id: str, body: ScoreIn):
    u = authz.need_login(user(request))
    with db.tx() as conn:
        return {"id": scoring.save(conn, u, assignment_id, body.criteria, body.comment.strip())}


@router.get("/export/scores.csv", tags=["organizer"], summary="Every review in the events I organize",
            response_class=Response)
def export_scores(request: Request, event: str | None = None):
    u = authz.need_login(user(request))
    with db.tx() as conn:
        if event:
            ev_id = db.val(conn, "SELECT id FROM events WHERE id = %s OR slug = %s", (event, event))
            authz.need_organizer(conn, u, ev_id)               # 403 unless you organize this one
            events = [ev_id]
        else:
            events = authz.organized_events(conn, u)
            if not events:
                raise forbidden("organizers only")
        text = scoring.csv_for(conn, events)
        audit.log(conn, u, "export.scores", events[0] if len(events) == 1 else None, ",".join(events))
    return Response(text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="scores.csv"'})


@router.get("/events/{event}/results", tags=["public"], summary="Published results (frozen snapshot)")
def published_results(event: str):
    from ..errors import not_found
    from ..services import results
    with db.tx() as conn:
        ev = db.one(conn, "SELECT id FROM events WHERE id = %s OR slug = %s", (event, event))
        snap = results.published(conn, ev["id"]) if ev else None
    if snap is None:
        raise not_found("results are not published", "not_published")
    return {**snap["body"], "version": snap["version"], "sha256": snap["body_hash"]}


class ChoiceIn(BaseModel):
    winner: str = Field(..., description="the id of the better entry, or 'tie'")


@router.get("/judge/pairs", tags=["judging"], summary="My head-to-head comparisons (pairwise mode)")
def my_pairs(request: Request):
    u = authz.need_login(user(request))
    with db.tx() as conn:
        rows = db.rows(conn, """SELECT c.id, c.event_id, c.project_a, c.project_b, c.winner FROM comparisons c
                                JOIN memberships m ON m.event_id = c.event_id AND m.user_id = c.judge_id AND m.role = 'judge'
                                WHERE c.judge_id = %s ORDER BY c.created_at, c.id""", (u.id,))
        return {"pairs": [{**r, "winner": {"a": r["project_a"], "b": r["project_b"], "tie": "tie"}.get(r["winner"])}
                          for r in rows]}


@router.put("/pairs/{cid}", tags=["judging"], summary="Answer a comparison: which entry is better")
def answer_pair(request: Request, cid: str, body: ChoiceIn):
    from ..services import pairwise
    u = authz.need_login(user(request))
    with db.tx() as conn:
        pairwise.decide(conn, u, cid, body.winner)
    return {"ok": True}
