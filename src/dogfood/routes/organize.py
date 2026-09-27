"""The organizer's side: create an event, configure it, invite judges, assign, watch progress, publish.

Every handler starts with authz.need_organizer for the event in the URL. Organizing one event gives
no rights in another.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import auth, authz, db
from ..errors import forbidden, not_found
from ..services import assign, events, results, voting
from ..services.submissions import is_open
from ..web import form_or_json, go, page, wants_json

router = APIRouter(prefix="/organize", include_in_schema=False)


def _event(conn, me, slug):
    ev = db.one(conn, "SELECT * FROM events WHERE slug = %s OR id = %s", (slug, slug))
    if ev is None:
        raise not_found("no such event")
    authz.need_organizer(conn, me, ev["id"])
    return ev


def _done(request, url, message, payload=None, status=200):
    if wants_json(request):
        return JSONResponse(payload if payload is not None else {"ok": True}, status_code=status)
    return go(url, message)


@router.get("")
def index(request: Request):
    me = auth.need_user(request)
    with db.tx() as conn:
        ids = authz.organized_events(conn, me)
        if not ids and not me.is_admin:
            raise forbidden("you do not organize any event", "not_an_organizer")
        evs = db.rows(conn, """SELECT e.*, (SELECT count(*) FROM projects p WHERE p.event_id = e.id
                                             AND p.status = 'submitted') AS submitted,
                                  (SELECT count(*) FROM assignments a WHERE a.event_id = e.id) AS assigned,
                                  (SELECT count(*) FROM scores s WHERE s.event_id = e.id) AS scored
                               FROM events e WHERE e.id = ANY(%s) ORDER BY e.created_at DESC""", (ids,))
    return page(request, "organize/index.html", events=evs)


@router.post("/events")
async def create_event(request: Request):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        if not me.is_admin and not authz.organized_events(conn, me):
            raise forbidden("ask an admin to make you an organizer first", "not_an_organizer")
        ev = events.create(conn, me, data)
    return _done(request, f"/organize/{ev['slug']}", "Event created. Next: invite judges.",
                 {"id": ev["id"], "slug": ev["slug"]}, 201)


@router.get("/{slug}")
def dashboard(request: Request, slug: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        counts = db.one(conn, """SELECT
            (SELECT count(*) FROM teams WHERE event_id = %(e)s) AS teams,
            (SELECT count(*) FROM team_members WHERE event_id = %(e)s) AS people,
            count(*) FILTER (WHERE status = 'submitted') AS submitted, count(*) FILTER (WHERE status = 'draft') AS drafts,
            count(*) FILTER (WHERE status = 'withdrawn') AS withdrawn, count(*) FILTER (WHERE status = 'duplicate') AS duplicates
            FROM projects WHERE event_id = %(e)s""", {"e": ev["id"]})
        judges = db.rows(conn, """
            SELECT u.id, u.name, u.email, u.password_hash IS NOT NULL AS active,
                   (SELECT string_agg(t.name, ', ' ORDER BY t.name) FROM judge_tracks jt JOIN tracks t ON t.id = jt.track_id
                     WHERE jt.user_id = u.id AND jt.event_id = %(e)s) AS tracks,
                   (SELECT count(*) FROM assignments a WHERE a.judge_id = u.id AND a.event_id = %(e)s) AS assigned,
                   (SELECT count(*) FROM scores s WHERE s.judge_id = u.id AND s.event_id = %(e)s) AS done
            FROM memberships m JOIN users u ON u.id = m.user_id
            WHERE m.event_id = %(e)s AND m.role = 'judge' ORDER BY u.name""", {"e": ev["id"]})
        organizers = db.rows(conn, """SELECT u.name, u.email FROM memberships m JOIN users u ON u.id = m.user_id
                                      WHERE m.event_id = %s AND m.role = 'organizer' ORDER BY u.name""", (ev["id"],))
        rubric = db.rows(conn, "SELECT * FROM criteria WHERE event_id = %s ORDER BY position", (ev["id"],))
        tracks = db.rows(conn, "SELECT * FROM tracks WHERE event_id = %s ORDER BY name", (ev["id"],))
        scored = db.val(conn, "SELECT count(*) FROM scores WHERE event_id = %s", (ev["id"],))
        entries = db.rows(conn, """SELECT p.id, p.title, p.status, p.duplicate_of, t.name AS team,
                                         (SELECT count(*) FROM scores s WHERE s.project_id = p.id) AS reviews
                                  FROM projects p JOIN teams t ON t.id = p.team_id WHERE p.event_id = %s
                                  ORDER BY p.status <> 'submitted', p.title""", (ev["id"],))
        res = results.compute(conn, ev["id"])
        pub = results.published(conn, ev["id"])
        tally = None if voting.sealed(ev) else voting.tally(conn, ev)
        voters = db.val(conn, "SELECT count(DISTINCT voter_id) FROM votes WHERE event_id = %s", (ev["id"],))
    flags = {j["judge_id"]: j.get("flag") for j in res["judges"]}
    total = sum(j["assigned"] for j in judges)
    progress = {"assigned": total, "done": sum(j["done"] for j in judges), "reviews": scored,
                "pct": round(100 * sum(j["done"] for j in judges) / total) if total else 0}
    if wants_json(request):
        return JSONResponse({"event": ev["id"], "counts": counts, "progress": progress,
                             "judges": [{**{k: j[k] for k in ("id", "name", "assigned", "done")}, "flag": flags.get(j["id"])}
                                        for j in judges], "flags": res["flags"]})
    return page(request, "organize/dashboard.html", ev=ev, counts=counts, judges=judges, flags=flags,
                organizers=organizers, rubric=rubric, tracks=tracks, scored=scored, res=res, pub=pub,
                progress=progress, open=is_open(ev), entries=entries, tally=tally, voters=voters)


@router.post("/{slug}/settings")
async def settings(request: Request, slug: str):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        events.update_settings(conn, me, ev, data)
    return _done(request, f"/organize/{slug}#settings", "Settings saved.")


@router.post("/{slug}/rubric")
async def rubric(request: Request, slug: str):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        events.update_rubric(conn, me, ev, data)
    return _done(request, f"/organize/{slug}#rubric", "Rubric saved. The results preview uses it immediately.")


@router.post("/{slug}/people")
async def invite(request: Request, slug: str):
    me = auth.need_user(request)
    form = await request.form() if not request.headers.get("content-type", "").startswith("application/json") else None
    data = await form_or_json(request)
    tracks = form.getlist("tracks") if form is not None else list(data.get("tracks") or [])
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        out = events.invite(conn, me, ev, data.get("email", ""), data.get("role", "judge"), tracks, data.get("name", ""))
    if wants_json(request):
        return JSONResponse(out, status_code=201)
    if out["link"]:
        return go(f"/organize/{slug}#judges", f"Added {out['email']} as {out['role']}. Send them this one-time link "
                  f"to set a password: {request.base_url}{out['link'].lstrip('/')}")
    return go(f"/organize/{slug}#judges", f"Added {out['email']} as {out['role']} (they already have an account).")


@router.post("/{slug}/judges/{uid}/remove")
def remove_judge(request: Request, slug: str, uid: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        out = events.remove_judge(conn, me, ev, uid)
    return _done(request, f"/organize/{slug}#judges",
                 f"Judge removed. {out['freed']} unscored assignment(s) returned to the pool; run the planner to refill.", out)


@router.post("/{slug}/assign")
def run_planner(request: Request, slug: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        out = assign.run(conn, me, ev)
    msg = f"Created {out['created']} assignment(s)."
    if out["shortfalls"]:
        msg += f" {len(out['shortfalls'])} project(s) cannot reach {ev['reviews_per_project']} reviews with the current judges."
    return _done(request, f"/organize/{slug}#judging", msg, out)


@router.get("/{slug}/assignments")
def assignments(request: Request, slug: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        rows = db.rows(conn, """SELECT a.id, a.batch, a.project_id, p.title, p.status, u.name AS judge, a.judge_id,
                                       s.id IS NOT NULL AS scored
                                FROM assignments a JOIN projects p ON p.id = a.project_id JOIN users u ON u.id = a.judge_id
                                LEFT JOIN scores s ON s.assignment_id = a.id
                                WHERE a.event_id = %s ORDER BY p.title, u.name""", (ev["id"],))
        judges = db.rows(conn, """SELECT u.id, u.name FROM memberships m JOIN users u ON u.id = m.user_id
                                  WHERE m.event_id = %s AND m.role = 'judge' ORDER BY u.name""", (ev["id"],))
        projects = db.rows(conn, "SELECT id, title FROM projects WHERE event_id = %s AND status = 'submitted' "
                                 "ORDER BY title", (ev["id"],))
    if wants_json(request):
        return JSONResponse({"assignments": rows})
    return page(request, "organize/assignments.html", ev=ev, rows=rows, judges=judges, projects=projects)


@router.post("/{slug}/assignments")
async def add_assignment(request: Request, slug: str):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        aid = assign.assign_one(conn, me, ev, data.get("judge_id", ""), data.get("project_id", ""))
    return _done(request, f"/organize/{slug}/assignments", "Assigned.", {"id": aid}, 201)


@router.post("/{slug}/assignments/{aid}/remove")
def remove_assignment(request: Request, slug: str, aid: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        assign.unassign(conn, me, ev, aid)
    return _done(request, f"/organize/{slug}/assignments", "Assignment removed.")


@router.post("/{slug}/projects/{pid}/status")
async def moderate(request: Request, slug: str, pid: str):
    """Withdraw an entry (rules breach, team request) or restore it. Allowed after the deadline: the
    database trigger lets status-only changes through."""
    from .. import audit
    from ..errors import bad, conflict
    me = auth.need_user(request)
    data = await form_or_json(request)
    status = data.get("status")
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        p = db.one(conn, "SELECT * FROM projects WHERE id = %s AND event_id = %s FOR UPDATE", (pid, ev["id"]))
        if p is None:
            raise not_found("no such project in this event")
        if ev["results_published_at"]:
            raise conflict("results are published; retract them first")
        if status not in ("withdrawn", "submitted") or (status == "submitted" and p["status"] != "withdrawn"):
            raise bad("an organizer can withdraw a submitted entry or restore a withdrawn one")
        conn.execute("UPDATE projects SET status = %s WHERE id = %s", (status, pid))
        audit.log(conn, me, f"project.{'withdraw' if status == 'withdrawn' else 'restore'}", ev["id"], pid,
                  {"reason": (data.get("reason") or "").strip()[:300]})
    return _done(request, f"/organize/{slug}#entries", "Entry updated.")


@router.get("/{slug}/results")
def preview(request: Request, slug: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        res = results.compute(conn, ev["id"])
        pub = results.published(conn, ev["id"])
    if wants_json(request):
        return JSONResponse(res)
    return page(request, "organize/results.html", ev=ev, res=res, pub=pub)


@router.post("/{slug}/publish")
async def publish(request: Request, slug: str):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        out = results.publish(conn, me, ev, force=str(data.get("force", "")).lower() in ("1", "true", "on", "yes"))
    return _done(request, f"/events/{ev['slug']}/results", f"Results published (version {out['version']}).", out, 201)


@router.post("/{slug}/retract")
async def retract(request: Request, slug: str):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        results.retract(conn, me, ev, data.get("reason", ""))
    return _done(request, f"/organize/{slug}#publish", "Results retracted. Judging is open again.")


@router.get("/{slug}/audit")
def audit_view(request: Request, slug: str):
    from .. import audit
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, me, slug)
        rows = db.rows(conn, """SELECT l.id, l.at, l.action, l.subject, l.detail, coalesce(u.name, l.actor, 'system') AS who
                                FROM audit_log l LEFT JOIN users u ON u.id = l.actor
                                WHERE l.event_id = %s ORDER BY l.id DESC LIMIT 300""", (ev["id"],))
        ok, n, bad_id = audit.verify(conn)
    if wants_json(request):
        return JSONResponse({"chain_ok": ok, "entries": n, "first_bad": bad_id,
                             "log": [{**r, "at": r["at"].isoformat()} for r in rows]})
    return page(request, "organize/audit.html", ev=ev, rows=rows, ok=ok, n=n, bad_id=bad_id)
