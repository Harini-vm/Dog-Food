"""Tier 4: the public API, personal tokens, webhooks, certificates, the embeddable widget, import/export."""

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import audit, auth, authz, db
from ..errors import bad, forbidden, not_found
from ..security import digest, new_id, new_token
from ..services import certificates, importer, results, webhooks
from ..web import form_or_json, go, page, wants_json

api = APIRouter(prefix="/api/v1")
router = APIRouter(include_in_schema=False)


def _event(conn, key):
    ev = db.one(conn, "SELECT * FROM events WHERE id = %s OR slug = %s", (key, key))
    if ev is None:
        raise not_found("no such event")
    return ev


# ---------------------------------------------------------------- public read API

@api.get("/events", tags=["public"], summary="All events")
def events_list():
    with db.tx() as conn:
        rows = db.rows(conn, """SELECT id, slug, name, description, opens_at, closes_at, voting_opens_at, voting_closes_at,
                                       results_published_at IS NOT NULL AS results_published
                                FROM events ORDER BY created_at DESC""")
    return {"events": rows}


@api.get("/events/{event}/projects", tags=["public"],
         summary="Submitted projects, cursor-paginated (stable while new projects arrive)")
def projects_page(event: str, cursor: str = "", limit: int = 20, track: str = ""):
    """Keyset pagination on id: pass `next_cursor` back as `cursor`. Unlike offset paging, a project
    submitted between two requests never causes a duplicate or a skipped item."""
    limit = max(1, min(limit, 100))
    with db.tx() as conn:
        ev = _event(conn, event)
        rows = db.rows(conn, """SELECT p.id, p.title, p.summary, p.repo_url, p.track_id, t.name AS track, tm.name AS team,
                                       p.submitted_at
                                FROM projects p JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks t ON t.id = p.track_id
                                WHERE p.event_id = %s AND p.status = 'submitted' AND p.id > %s
                                  AND (%s = '' OR p.track_id = %s)
                                ORDER BY p.id LIMIT %s""", (ev["id"], cursor, track, track, limit + 1))
    more = len(rows) > limit
    rows = rows[:limit]
    return {"projects": rows, "next_cursor": rows[-1]["id"] if more else None}


@api.get("/projects/{pid}", tags=["public"], summary="One project (drafts and withdrawn entries: 404)")
def project_one(request: Request, pid: str):
    with db.tx() as conn:
        p = db.one(conn, """SELECT p.id, p.event_id, p.team_id, p.title, p.summary, p.repo_url, p.status, p.track_id,
                                   tm.name AS team, p.submitted_at
                            FROM projects p JOIN teams tm ON tm.id = p.team_id WHERE p.id = %s""", (pid,))
        if p is None or not authz.project_visible(conn, auth.user(request), p):
            raise not_found("no such project")               # same answer for hidden and missing: no probing
    return p


@router.get("/api/docs")
def api_docs(request: Request):
    return page(request, "apidocs.html")


# ---------------------------------------------------------------- personal API tokens

@router.get("/account")
def account(request: Request):
    me = auth.need_user(request)
    with db.tx() as conn:
        tokens = db.rows(conn, "SELECT id, label, created_at, revoked_at FROM api_tokens WHERE user_id = %s "
                               "AND label NOT LIKE 'demo:%%' ORDER BY created_at DESC", (me.id,))
    return page(request, "account.html", tokens=tokens, new=None)


@api.post("/tokens", tags=["identity"], summary="Create a personal API token (shown once)")
@router.post("/account/tokens")
async def token_create(request: Request):
    me = auth.need_user(request)
    data = await form_or_json(request)
    label = (data.get("label") or "").strip()[:60] or "token"
    raw = "df_" + new_token(24)
    with db.tx() as conn:
        tid = new_id("tok")
        conn.execute("INSERT INTO api_tokens (id, user_id, label, token_hash) VALUES (%s, %s, %s, %s)",
                     (tid, me.id, label, digest(raw)))
        audit.log(conn, me, "token.create", None, tid)
        tokens = db.rows(conn, "SELECT id, label, created_at, revoked_at FROM api_tokens WHERE user_id = %s "
                               "AND label NOT LIKE 'demo:%%' ORDER BY created_at DESC", (me.id,))
    if wants_json(request):
        return JSONResponse({"id": tid, "token": raw}, status_code=201)
    return page(request, "account.html", tokens=tokens, new=raw)


@api.delete("/tokens/{tid}", tags=["identity"], summary="Revoke one of my tokens")
@router.post("/account/tokens/{tid}/revoke")
def token_revoke(request: Request, tid: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        n = conn.execute("UPDATE api_tokens SET revoked_at = now() WHERE id = %s AND user_id = %s AND revoked_at IS NULL",
                         (tid, me.id)).rowcount
        if not n:
            raise not_found("no such active token")
        audit.log(conn, me, "token.revoke", None, tid)
    return JSONResponse({"ok": True}) if wants_json(request) else go("/account", "Token revoked.")


# ---------------------------------------------------------------- webhooks (organizers)

@router.get("/organize/{slug}/webhooks")
def webhooks_page(request: Request, slug: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, slug)
        authz.need_organizer(conn, me, ev["id"])
        hooks = db.rows(conn, "SELECT id, url, active, created_at FROM webhooks WHERE event_id = %s ORDER BY created_at",
                        (ev["id"],))
        deliveries = db.rows(conn, """SELECT d.*, e.type, e.id AS envelope, w.url FROM webhook_deliveries d
                                      JOIN webhook_events e ON e.sequence = d.sequence JOIN webhooks w ON w.id = d.webhook_id
                                      WHERE w.event_id = %s ORDER BY d.sequence DESC LIMIT 100""", (ev["id"],))
    if wants_json(request):
        return JSONResponse(json.loads(json.dumps({"webhooks": hooks, "deliveries": deliveries}, default=str)))
    return page(request, "organize/webhooks.html", ev=ev, hooks=hooks, deliveries=deliveries, secret=None)


@router.post("/organize/{slug}/webhooks")
async def webhook_add(request: Request, slug: str):
    me = auth.need_user(request)
    data = await form_or_json(request)
    with db.tx() as conn:
        ev = _event(conn, slug)
        authz.need_organizer(conn, me, ev["id"])
        out = webhooks.add(conn, me, ev, data.get("url", ""))
    if wants_json(request):
        return JSONResponse(out, status_code=201)
    return go(f"/organize/{slug}/webhooks", f"Webhook added. Its signing secret (shown only now): {out['secret']}")


@router.post("/organize/{slug}/webhooks/ping")
def webhook_ping(request: Request, slug: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, slug)
        authz.need_organizer(conn, me, ev["id"])
        webhooks.emit(conn, ev["id"], "ping", {"by": me.name})
    return JSONResponse({"ok": True}, status_code=202) if wants_json(request) else \
        go(f"/organize/{slug}/webhooks", "Ping queued. Refresh to see the delivery.")


@router.post("/organize/{slug}/webhooks/{wid}/remove")
def webhook_remove(request: Request, slug: str, wid: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, slug)
        authz.need_organizer(conn, me, ev["id"])
        webhooks.remove(conn, me, ev, wid)
    return JSONResponse({"ok": True}) if wants_json(request) else go(f"/organize/{slug}/webhooks", "Webhook removed.")


# ---------------------------------------------------------------- certificates

@router.post("/organize/{slug}/certificates")
def certificates_issue(request: Request, slug: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, slug)
        authz.need_organizer(conn, me, ev["id"])
        out = certificates.issue(conn, me, ev)
    return JSONResponse(out, status_code=201) if wants_json(request) else \
        go(f"/organize/{slug}/certificates", f"Issued {out['issued']} signed certificates.")


@router.get("/organize/{slug}/certificates")
def certificates_list(request: Request, slug: str):
    me = auth.need_user(request)
    with db.tx() as conn:
        ev = _event(conn, slug)
        authz.need_organizer(conn, me, ev["id"])
        rows = db.rows(conn, """SELECT id, kind, record->>'recipient' AS recipient, record->>'team' AS team,
                                       record->'project'->>'title' AS title, record->>'place' AS place, record->>'track' AS track
                                FROM certificates WHERE event_id = %s ORDER BY kind, recipient""", (ev["id"],))
    if wants_json(request):
        return JSONResponse({"certificates": rows})
    return page(request, "organize/certificates.html", ev=ev, rows=rows)


@router.get("/certificates/{cid}")
def certificate_page(request: Request, cid: str):
    with db.tx() as conn:
        c = certificates.get(conn, cid)
        if c is None:
            raise not_found("no such certificate")
        status = certificates.verify(conn, c["record"], c["signature"])
    return page(request, "certificate.html", c=c, rec=c["record"], status=status)


@api.get("/certificates/{cid}", tags=["certificates"], summary="A signed record and its signature")
def certificate_json(cid: str):
    with db.tx() as conn:
        c = certificates.get(conn, cid)
    if c is None:
        raise not_found("no such certificate")
    return {"record": c["record"], "signature": c["signature"], "algorithm": "Ed25519",
            "signed_bytes": "canonical JSON: sorted keys, no whitespace, UTF-8", "keys": "/.well-known/dogfood-keys.json"}


@api.post("/certificates/verify", tags=["certificates"], summary="Check a record and signature")
async def certificate_verify(request: Request):
    try:
        data = await request.json()
    except ValueError:
        raise bad("send JSON {record, signature}")
    with db.tx() as conn:
        return certificates.verify(conn, (data or {}).get("record"), (data or {}).get("signature"))


@router.get("/.well-known/dogfood-keys.json")
def keys():
    return certificates.public_keys()


# ---------------------------------------------------------------- embeddable widget

@router.get("/embed/{event}")
def embed(request: Request, event: str, view: str = "gallery", track: str = ""):
    """A read-only, script-free page other sites may put in an iframe. Only /embed/* may be framed."""
    with db.tx() as conn:
        ev = _event(conn, event)
        snap = results.published(conn, ev["id"]) if view == "results" else None
        items = db.rows(conn, """SELECT p.id, p.title, tm.name AS team, t.name AS track FROM projects p
                                 JOIN teams tm ON tm.id = p.team_id LEFT JOIN tracks t ON t.id = p.track_id
                                 WHERE p.event_id = %s AND p.status = 'submitted' AND (%s = '' OR p.track_id = %s)
                                 ORDER BY p.title LIMIT 100""", (ev["id"], track, track)) if view != "results" else []
    return page(request, "embed.html", ev=ev, view=view, snap=snap, items=items)


# ---------------------------------------------------------------- import / export

@api.get("/events/{event}/export", tags=["organizer"], summary="Full event export (re-importable)")
def export(request: Request, event: str):
    me = authz.need_login(auth.user(request))
    with db.tx() as conn:
        ev = _event(conn, event)
        authz.need_organizer(conn, me, ev["id"])
        data = importer.export_event(conn, ev["id"])
        audit.log(conn, me, "event.export", ev["id"], ev["id"])
    return Response(json.dumps(data, indent=2, ensure_ascii=False), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{ev["slug"]}.json"'})


@api.post("/events/import", tags=["organizer"], summary="Import an event (admins). ?as_copy=true for fresh ids")
@router.post("/organize/import")
async def do_import(request: Request, as_copy: bool = False):
    me = authz.need_login(auth.user(request))
    if not me.is_admin:
        raise forbidden("only admins can import events")
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/"):
        form = await request.form()
        f = form.get("file")
        raw = await f.read() if f is not None and hasattr(f, "read") else b""
        as_copy = as_copy or form.get("as_copy") in ("1", "on", "true")
    else:
        raw = await request.body()
    if len(raw) > 20_000_000:
        raise bad("file is larger than 20 MB")
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise bad("that is not a JSON file")
    with db.tx() as conn:
        report = importer.import_event(conn, data, actor=me, organizers=[me.id], as_copy=as_copy)
    if wants_json(request):
        return JSONResponse(report, status_code=201)
    return go(f"/organize/{report['slug']}", f"Imported {report['projects']} projects and {report['scores']} reviews.")
