"""The judge's queue and score form. A judge only ever sees their own assignments and scores."""

from fastapi import APIRouter, Request

from .. import auth, db
from ..errors import forbidden
from ..services import pairwise, scoring
from ..web import form_or_json, go, page

router = APIRouter(prefix="/judge", include_in_schema=False)


@router.get("")
def queue(request: Request):
    me = auth.need_user(request)
    with db.tx() as conn:
        rows = db.rows(conn, """
            SELECT a.id, a.project_id, p.title, p.summary, p.status, t.name AS track, e.name AS event, e.slug,
                   e.results_published_at, e.judging_closes_at, s.id IS NOT NULL AS scored, s.updated_at
            FROM assignments a JOIN projects p ON p.id = a.project_id JOIN events e ON e.id = a.event_id
            JOIN memberships m ON m.event_id = a.event_id AND m.user_id = a.judge_id AND m.role = 'judge'
            LEFT JOIN tracks t ON t.id = p.track_id LEFT JOIN scores s ON s.assignment_id = a.id
            WHERE a.judge_id = %s ORDER BY e.created_at DESC, s.id IS NOT NULL, p.title""", (me.id,))
        judges_anything = db.one(conn, "SELECT 1 FROM memberships WHERE user_id = %s AND role = 'judge'", (me.id,))
        pairs = pairwise.for_judge(conn, me)
    if not judges_anything:
        raise forbidden("this page is for judges", "not_a_judge")
    done = sum(r["scored"] for r in rows)
    return page(request, "judge/queue.html", rows=rows, done=done, total=len(rows), pairs=pairs,
                pairs_open=sum(1 for c in pairs if c["winner"] is None))


@router.get("/pairs/{cid}")
def pair_form(request: Request, cid: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        c = pairwise.load(conn, me, cid)
        left, right = pairwise.project_pair(conn, c)
        ev = db.one(conn, "SELECT * FROM events WHERE id = %s", (c["event_id"],))
        nxt = db.val(conn, """SELECT id FROM comparisons WHERE judge_id = %s AND winner IS NULL AND id <> %s
                              ORDER BY created_at, id LIMIT 1""", (me.id, cid))
    chosen = {"a": c["project_a"], "b": c["project_b"], "tie": "tie"}.get(c["winner"] or "", None)
    return page(request, "judge/pair.html", c=c, left=left, right=right, ev=ev, nxt=nxt, chosen=chosen,
                open=scoring.judging_open(ev))


@router.post("/pairs/{cid}")
async def pair_save(request: Request, cid: str):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        pairwise.decide(conn, me, cid, data.get("choice", ""))
    return go(f"/judge/pairs/{data['next']}" if data.get("next") else "/judge#pairs", "Comparison saved.")


def _assignment(conn, me, aid):
    a = db.one(conn, """SELECT a.*, p.title, p.summary, p.repo_url, p.status, t.name AS track, tm.name AS team,
                               e.name AS event, e.slug, e.results_published_at, e.judging_closes_at
                        FROM assignments a JOIN projects p ON p.id = a.project_id JOIN teams tm ON tm.id = p.team_id
                        JOIN events e ON e.id = a.event_id LEFT JOIN tracks t ON t.id = p.track_id
                        WHERE a.id = %s""", (aid,))
    if a is None or a["judge_id"] != me.id:               # same answer for "missing" and "someone else's"
        raise forbidden("this is not your assignment")
    return a


@router.get("/{aid}")
def form(request: Request, aid: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        a = _assignment(conn, me, aid)
        rubric = scoring.criteria(conn, a["event_id"])
        s = db.one(conn, "SELECT * FROM scores WHERE assignment_id = %s", (aid,))
        values = {r["key"]: r["value"] for r in db.rows(conn, """SELECT c.key, i.value FROM score_items i
                  JOIN criteria c ON c.id = i.criterion_id WHERE i.score_id = %s""", (s["id"],))} if s else {}
        nxt = db.val(conn, """SELECT a.id FROM assignments a LEFT JOIN scores s ON s.assignment_id = a.id
                              WHERE a.judge_id = %s AND a.event_id = %s AND s.id IS NULL AND a.id <> %s
                              ORDER BY a.assigned_at, a.id LIMIT 1""", (me.id, a["event_id"], aid))
    ev = {"results_published_at": a["results_published_at"], "judging_closes_at": a["judging_closes_at"]}
    return page(request, "judge/score.html", a=a, rubric=rubric, s=s, values=values, nxt=nxt,
                open=scoring.judging_open(ev))


@router.post("/{aid}")
async def save(request: Request, aid: str):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        rubric = scoring.criteria(conn, _assignment(conn, me, aid)["event_id"])
        values = {}
        for c in rubric:
            try:
                values[c["key"]] = int(data.get(c["key"], ""))
            except ValueError:
                values[c["key"]] = None                    # scoring.save explains what is missing
        scoring.save(conn, me, aid, values, (data.get("comment") or "").strip())
    target = f"/judge/{data['next']}" if data.get("next") else "/judge"
    return go(target, "Score saved.")
