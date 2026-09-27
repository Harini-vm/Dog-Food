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
    resp = templates.TemplateResponse(request, name, {"me": u, "csrf": (u.csrf if u else "") or "", "nav": nav,
                                                      "flash": json.loads(flash) if flash else None, **ctx},
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


async def form_or_json(request: Request) -> dict:
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            data = await request.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}
    return dict(await request.form())


def wants_json(request: Request) -> bool:
    return (request.url.path.startswith("/api/") or bool(request.headers.get("authorization"))
            or request.headers.get("content-type", "").startswith("application/json"))
