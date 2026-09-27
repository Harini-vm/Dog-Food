"""Public pages, login, and the participant's entry form."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import auth, authz, db
from ..errors import forbidden, not_found
from ..security import check_password
from ..services import submissions
from ..web import form_or_json, go, page, wants_json

router = APIRouter(include_in_schema=False)


@router.get("/")
def home(request: Request):
    with db.tx() as conn:
        events = db.rows(conn, """SELECT e.*, (SELECT count(*) FROM projects p WHERE p.event_id = e.id
                                                AND p.status = 'submitted') AS n
                                  FROM events e ORDER BY created_at DESC""")
    return page(request, "home.html", events=events)


@router.get("/events/{slug}")
def event(request: Request, slug: str):
    with db.tx() as conn:
        ev = db.one(conn, "SELECT * FROM events WHERE slug = %s", (slug,)) or _missing()
        tracks = db.rows(conn, "SELECT * FROM tracks WHERE event_id = %s ORDER BY name", (ev["id"],))
        me = auth.user(request)
        team = submissions.team_of(conn, me.id, ev["id"]) if me else None
    return page(request, "event.html", ev=ev, tracks=tracks, team=team, open=submissions.is_open(ev))


@router.get("/projects")
def gallery(request: Request, q: str = "", event: str = "", track: str = ""):
    """Public, no login. Only submitted entries; duplicates, drafts and withdrawn entries never show."""
    like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    with db.tx() as conn:
        items = db.rows(conn, """
            SELECT p.id, p.title, p.summary, p.repo_url, t.name AS track, tm.name AS team, e.name AS event, e.slug
            FROM projects p JOIN events e ON e.id = p.event_id JOIN teams tm ON tm.id = p.team_id
            LEFT JOIN tracks t ON t.id = p.track_id
            WHERE p.status = 'submitted' AND (%(e)s = '' OR e.slug = %(e)s) AND (%(t)s = '' OR p.track_id = %(t)s)
              AND (%(q)s = '' OR p.title ILIKE %(l)s OR p.summary ILIKE %(l)s OR tm.name ILIKE %(l)s)
            ORDER BY e.created_at DESC, p.id LIMIT 200""", {"e": event, "t": track, "q": q.strip(), "l": like})
        events = db.rows(conn, "SELECT slug, name FROM events ORDER BY created_at DESC")
        tracks = db.rows(conn, """SELECT t.id, t.name FROM tracks t JOIN events e ON e.id = t.event_id
                                  WHERE %s = '' OR e.slug = %s ORDER BY t.name""", (event, event))
    if wants_json(request):
        return JSONResponse({"projects": items})
    return page(request, "gallery.html", items=items, q=q, event=event, track=track, events=events, tracks=tracks)


@router.get("/projects/new")
def new_entry(request: Request, event: str = ""):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event_for(conn, me, event)
        team = submissions.team_of(conn, me.id, ev["id"])
        entry = team and db.one(conn, "SELECT * FROM projects WHERE team_id = %s AND status IN ('draft','submitted')",
                                (team["id"],))
        tracks = db.rows(conn, "SELECT * FROM tracks WHERE event_id = %s ORDER BY name", (ev["id"],))
    return page(request, "entry.html", ev=ev, team=team, entry=entry, tracks=tracks, open=submissions.is_open(ev))


@router.post("/projects/new")
async def save_entry(request: Request):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        ev = _event_for(conn, me, data.get("event", ""))
        pid = submissions.save(conn, me, ev, data, submit=data.get("action") == "submit")
    if wants_json(request):
        return JSONResponse({"id": pid}, status_code=201)
    return go(f"/projects/{pid}", "Saved.")


@router.get("/projects/{pid}")
def project(request: Request, pid: str):
    me = auth.user(request)
    with db.tx() as conn:
        p = db.one(conn, """SELECT p.*, t.name AS track, tm.name AS team, e.name AS event, e.slug
                            FROM projects p JOIN teams tm ON tm.id = p.team_id JOIN events e ON e.id = p.event_id
                            LEFT JOIN tracks t ON t.id = p.track_id WHERE p.id = %s""", (pid,)) or _missing()
        if not authz.project_visible(conn, me, p):
            _missing()                                   # drafts are private; same answer as "no such project"
        members = db.rows(conn, """SELECT u.name FROM team_members m JOIN users u ON u.id = m.user_id
                                   WHERE m.team_id = %s ORDER BY m.captain DESC, u.name""", (p["team_id"],))
    return page(request, "project.html", p=p, members=members)


@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return page(request, "login.html", next=next)


@router.post("/login")
async def login(request: Request):
    data = await form_or_json(request)
    email = (data.get("email") or "").strip().lower()
    with db.tx() as conn:
        u = db.one(conn, "SELECT id, password_hash FROM users WHERE email = %s", (email,))
    if not check_password(data.get("password") or "", u["password_hash"] if u else None):
        return page(request, "login.html", 401, next=data.get("next", "/"), error="Wrong email or password.", email=email)
    target = data.get("next") or "/"
    resp = go(target if target.startswith("/") and not target.startswith("//") else "/")
    auth.login(resp, u["id"])
    return resp


@router.post("/logout")
def logout(request: Request):
    resp = go("/", "Logged out.")
    auth.logout(request, resp)
    return resp


def _event_for(conn, me, key):
    if key:
        return db.one(conn, "SELECT * FROM events WHERE slug = %s OR id = %s", (key, key)) or _missing()
    ev = db.one(conn, """SELECT e.* FROM team_members m JOIN events e ON e.id = m.event_id
                         WHERE m.user_id = %s ORDER BY e.created_at DESC LIMIT 1""", (me.id,))
    if ev is None:
        raise forbidden("join or create a team first", "no_team")
    return ev


def _missing():
    raise not_found("no such page")
