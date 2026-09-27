"""Accounts and teams: sign up, accept an invitation, create or join a team, leave it."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import audit, auth, db
from ..errors import bad, conflict, not_found
from ..security import hash_password, new_id
from ..services import events, teams
from ..web import form_or_json, go, page, wants_json

router = APIRouter(include_in_schema=False)


@router.get("/signup")
def signup_form(request: Request, next: str = "/"):
    return page(request, "signup.html", next=next)


@router.post("/signup")
async def signup(request: Request):
    data = await form_or_json(request)
    email = (data.get("email") or "").strip().lower()
    name = " ".join((data.get("name") or "").split())[:80]
    pw = data.get("password") or ""
    err = None
    if not events.EMAIL.match(email):
        err = "Enter a valid email address."
    elif not name:
        err = "Enter your name."
    elif len(pw) < 8:
        err = "Use a password of at least 8 characters."
    if err:
        if wants_json(request):
            raise bad(err)
        return page(request, "signup.html", 400, error=err, email=email, name=name, next=data.get("next", "/"))
    with db.tx() as conn:
        row = db.one(conn, "SELECT id, password_hash FROM users WHERE email = %s", (email,))
        if row and row["password_hash"]:
            if wants_json(request):
                raise conflict("an account with this email exists; log in instead", "email_taken")
            return page(request, "signup.html", 409, error="An account with this email exists. Log in instead.",
                        email=email, name=name, next=data.get("next", "/"))
        if row:
            # Imported or invited but never activated. Claiming it by signup would let anyone take
            # over a judge's account, so these people must use their invitation link.
            raise conflict("this email was invited; use the link from your organizer", "use_invite")
        uid = new_id("usr")
        conn.execute("INSERT INTO users (id, email, name, password_hash) VALUES (%s, %s, %s, %s)",
                     (uid, email, name, hash_password(pw)))
        audit.log(conn, uid, "user.signup", None, uid)
    target = data.get("next") or "/"
    resp = go(target if target.startswith("/") and not target.startswith("//") else "/", "Welcome! You are logged in.")
    auth.login(resp, uid)
    return resp


@router.get("/invite/{token}")
def invite_form(request: Request, token: str):
    return page(request, "invite.html", token=token)


@router.post("/invite/{token}")
async def accept_invite(request: Request, token: str):
    data = await form_or_json(request)
    with db.tx() as conn:
        inv = events.accept_invite(conn, token, data.get("name", ""), data.get("password", ""))
        slug = db.val(conn, "SELECT slug FROM events WHERE id = %s", (inv["event_id"],))
    resp = go("/judge" if inv["role"] == "judge" else f"/organize/{slug}", "Password set. Welcome aboard.")
    auth.login(resp, inv["user_id"])
    return resp


def _ev(conn, slug):
    ev = db.one(conn, "SELECT * FROM events WHERE slug = %s OR id = %s", (slug, slug))
    if ev is None:
        raise not_found("no such event")
    return ev


@router.post("/events/{slug}/teams")
async def create_team(request: Request, slug: str):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        t = teams.create(conn, me, _ev(conn, slug), data.get("name", ""))
    if wants_json(request):
        return JSONResponse({"id": t["id"], "name": t["name"], "invite_code": t["invite_code"]}, status_code=201)
    return go(f"/events/{slug}", f"Team {t['name']} created. Share the invite link with your teammates.")


@router.get("/join/{code}")
def join_form(request: Request, code: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        t = db.one(conn, """SELECT t.name, e.name AS event, e.slug, e.max_team_size,
                                   (SELECT count(*) FROM team_members m WHERE m.team_id = t.id) AS size
                            FROM teams t JOIN events e ON e.id = t.event_id WHERE t.invite_code = %s""", (code,))
    if t is None:
        raise not_found("that invite link does not match any team (it may have been changed)", "bad_invite_code")
    return page(request, "join.html", t=t, code=code, me_=me)


@router.post("/join/{code}")
def join(request: Request, code: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        t = teams.join(conn, me, code)
        slug = db.val(conn, "SELECT slug FROM events WHERE id = %s", (t["event_id"],))
    if wants_json(request):
        return JSONResponse({"team_id": t["id"], "name": t["name"]})
    return go(f"/events/{slug}", f"You joined {t['name']}.")


@router.post("/events/{slug}/team/leave")
def leave(request: Request, slug: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        teams.leave(conn, me, _ev(conn, slug))
    return go(f"/events/{slug}", "You left the team.")


@router.post("/events/{slug}/team/code")
def rotate(request: Request, slug: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        teams.rotate_code(conn, me, _ev(conn, slug))
    return go(f"/events/{slug}", "New invite link created; the old one no longer works.")
