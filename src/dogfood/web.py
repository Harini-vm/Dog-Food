"""Rendering helpers shared by the HTML routes."""

import json
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .auth import user

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["dt"] = lambda v, f="%d %b %Y, %H:%M UTC": v.strftime(f) if v else "—"


def page(request: Request, name: str, http_status: int = 200, **ctx):
    u = user(request)
    nav = {"judge": False, "organize": False}
    if u is not None:
        from . import db
        with db.tx() as conn:
            got = {r["role"] for r in db.rows(conn, "SELECT DISTINCT role FROM memberships WHERE user_id = %s", (u.id,))}
        nav = {"judge": "judge" in got, "organize": u.is_admin or "organizer" in got}
    flash = request.cookies.get("df_flash")
    try:
        flash_msg = json.loads(flash) if flash else None
        if not (isinstance(flash_msg, dict) and isinstance(flash_msg.get("text"), str)):
            flash_msg = None
    except ValueError:
        flash_msg = None
    resp = templates.TemplateResponse(request, name, {"me": u, "csrf": (u.csrf if u else "") or "", "nav": nav,
                                                      "flash": flash_msg, **ctx},
                                      status_code=http_status)
    if flash:
        resp.delete_cookie("df_flash", path="/")
    return resp


def go(url: str, message: str | None = None, kind: str = "ok") -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if message:
        resp.set_cookie("df_flash", json.dumps({"kind": kind, "text": message}), max_age=30, httponly=True,
                        samesite="lax", path="/")
    return resp


LIST_FIELDS = {"tracks"}


def _text(key, v):
    """Handlers expect text. Numbers and booleans become text; objects, stray lists and uploaded files
    in a text field become "" (and the handler's own validation then explains what is missing)."""
    if key in LIST_FIELDS and isinstance(v, list):          # e.g. judge tracks as a JSON list
        return [str(x) for x in v if isinstance(x, (str, int, float))]
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return str(v).lower()
    if isinstance(v, (int, float)):
        return str(v)
    return ""


async def form_or_json(request: Request) -> dict:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = await request.json()
        except ValueError:
            return {}
        return {k: _text(k, v) for k, v in data.items()} if isinstance(data, dict) else {}
    form = await request.form()
    return {k: _text(k, v) for k, v in form.items()}      # checkbox lists are read with form.getlist()


def wants_json(request: Request) -> bool:
    return (request.url.path.startswith("/api/") or bool(request.headers.get("authorization"))
            or request.headers.get("content-type", "").startswith("application/json"))
