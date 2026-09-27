"""Voting and comments, for browsers and for the API."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import auth, authz, config, db, ratelimit
from ..errors import forbidden, not_found
from ..services import comments, voting
from ..web import form_or_json, go, page, wants_json

router = APIRouter(include_in_schema=False)
api = APIRouter(prefix="/api/v1", tags=["public"])
COOKIE = "df_voter"


def _event(conn, key):
    ev = db.one(conn, "SELECT * FROM events WHERE slug = %s OR id = %s", (key, key))
    if ev is None:
        raise not_found("no such event")
    return ev


def _voter(conn, request, ev, create=True):
    """An account voter if logged in, else an email voter from a voting-link session."""
    me = auth.user(request)
    if me is not None:
        vid = voting.user_voter(conn, ev["id"], me.id) if create else db.val(
            conn, "SELECT id FROM voters WHERE event_id = %s AND user_id = %s", (ev["id"], me.id))
        return vid, None
    v = voting.session_voter(conn, request.cookies.get(COOKIE), ev["id"])
    return (v["id"], v["csrf"]) if v else (None, None)


@router.get("/events/{slug}/vote")
def ballot_page(request: Request, slug: str):
    with db.tx() as conn:
        ev = _event(conn, slug)
        st = voting.state(ev)
        vid, vcsrf = _voter(conn, request, ev, create=st == "open")
        items = voting.ballot(conn, ev, vid) if vid and st == "open" else []
        left = ev["votes_per_voter"] - voting.used(conn, vid) if vid else 0
        tally = voting.tally(conn, ev) if st == "closed" and not voting.sealed(ev) else None
    return page(request, "vote.html", ev=ev, st=st, items=items, left=left, has_voter=bool(vid), vcsrf=vcsrf,
                tally=tally, demo=config.DEMO)


@router.post("/events/{slug}/vote")
async def vote(request: Request, slug: str):
    data = await form_or_json(request)
    with db.tx() as conn:
        ev = _event(conn, slug)
        vid, vcsrf = _voter(conn, request, ev)
        if vid is None:
            raise forbidden("log in or open your voting link first", "no_voter")
        if vcsrf is not None and data.get("csrf") != vcsrf:          # email voters: their own CSRF token
            raise forbidden("form expired, reload the page", "csrf_failed")
        ratelimit.hit("vote", vid)
        ratelimit.hit("vote_net", ratelimit.client(request))
        pid = data.get("project_id", "")
        out = voting.withdraw(conn, vid, pid) if data.get("action") == "remove" else voting.cast(conn, vid, pid)
    if wants_json(request):
        return JSONResponse(out)
    return go(f"/events/{slug}/vote", "Vote removed." if data.get("action") == "remove" else
              f"Vote counted. {out['left']} left.")


@router.post("/events/{slug}/vote/link")
async def voting_link(request: Request, slug: str):
    data = await form_or_json(request)
    email = (data.get("email") or "").strip().lower()
    ratelimit.hit("link_net", ratelimit.client(request))
    ratelimit.hit("link_email", voting.email_key(email) if email else "none")
    with db.tx() as conn:
        link = voting.request_link(conn, _event(conn, slug), email, str(request.base_url))
    msg = "Check your email for a one-time voting link."
    if config.DEMO:
        msg += f" (Demo mode, no mail server: {link})"
    if wants_json(request):
        return JSONResponse({"sent": True, **({"link": link} if config.DEMO else {})}, status_code=202)
    return go(f"/events/{slug}/vote", msg)


@router.get("/vote/{token}")
def link_landing(request: Request, token: str):
    # Mail scanners open links to check them. A GET therefore only shows a button; the POST uses the link.
    return page(request, "vote_link.html", token=token)


@router.post("/vote/{token}")
def link_use(request: Request, token: str):
    with db.tx() as conn:
        row, session, _ = voting.use_link(conn, token)
        slug = db.val(conn, "SELECT slug FROM events WHERE id = %s", (row["event_id"],))
    resp = go(f"/events/{slug}/vote", "You can vote now.")
    resp.set_cookie(COOKIE, session, max_age=12 * 3600, httponly=True, samesite="lax",
                    secure=config.SECURE_COOKIES, path="/")
    return resp


# ---- comments (HTML) ----

@router.post("/projects/{pid}/comments")
async def add_comment(request: Request, pid: str):
    me = authz.need_login(auth.user(request))
    data = await form_or_json(request)
    ratelimit.hit("comment", me.id)
    with db.tx() as conn:
        cid = comments.add(conn, me, pid, data.get("body", ""))
    if wants_json(request):
        return JSONResponse({"id": cid}, status_code=201)
    return go(f"/projects/{pid}#c-{cid}", "Comment posted.")


@router.post("/comments/{cid}/{action}")
async def moderate_comment(request: Request, cid: str, action: str):
    me = authz.need_login(auth.user(request))
    if action not in ("hide", "restore"):
        raise not_found()
    data = await form_or_json(request)
    with db.tx() as conn:
        c = comments.moderate(conn, me, cid, action == "hide", data.get("reason", ""))
    return JSONResponse({"ok": True}) if wants_json(request) else go(f"/projects/{c['project_id']}#comments",
                                                                      "Comment updated.")


# ---- API ----

class VoteIn(BaseModel):
    project_id: str


@api.get("/events/{event}/ballot", summary="My ballot: every project once, in my own random order")
def api_ballot(request: Request, event: str):
    me = authz.need_login(auth.user(request))
    with db.tx() as conn:
        ev = _event(conn, event)
        if voting.state(ev) != "open":
            raise forbidden("voting is not open", "voting_closed")
        vid = voting.user_voter(conn, ev["id"], me.id)
        items = voting.ballot(conn, ev, vid)
        return {"votes_left": ev["votes_per_voter"] - voting.used(conn, vid), "projects": items}


@api.post("/events/{event}/votes", summary="Vote for a project")
def api_vote(request: Request, event: str, body: VoteIn):
    me = authz.need_login(auth.user(request))
    with db.tx() as conn:
        ev = _event(conn, event)
        vid = voting.user_voter(conn, ev["id"], me.id)
        ratelimit.hit("vote", vid)                 # the same bucket as the ballot page
        ratelimit.hit("vote_net", ratelimit.client(request))
        return voting.cast(conn, vid, body.project_id)


@api.delete("/events/{event}/votes/{project_id}", summary="Take a vote back while voting is open")
def api_unvote(request: Request, event: str, project_id: str):
    me = authz.need_login(auth.user(request))
    with db.tx() as conn:
        ev = _event(conn, event)
        return voting.withdraw(conn, voting.user_voter(conn, ev["id"], me.id), project_id)


@api.get("/events/{event}/tally", summary="Vote counts (403 tallies_sealed until voting closes)")
def api_tally(event: str):
    with db.tx() as conn:
        return {"tally": voting.tally(conn, _event(conn, event))}


@api.get("/projects/{pid}/comments", summary="Visible comments on a project")
def api_comments(request: Request, pid: str):
    with db.tx() as conn:
        p = db.one(conn, "SELECT * FROM projects WHERE id = %s", (pid,))
        if p is None or not authz.project_visible(conn, auth.user(request), p):
            raise not_found("no such project")
        return {"comments": [{"id": c["id"], "author": c["name"], "body": c["body"],
                              "created_at": c["created_at"].isoformat()} for c in comments.visible(conn, pid)]}
