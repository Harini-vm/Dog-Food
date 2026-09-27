"""Who is calling. Bearer tokens (API clients, the acceptance checker) or a session cookie (browsers).

Both are looked up by digest. A wrong bearer token is a 401 everywhere, so a misconfigured client
never silently browses as anonymous. Cookie-authenticated writes must echo the session's CSRF token.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import Request, Response

from . import config, db
from .errors import forbidden, unauthorized
from .security import digest, new_token

COOKIE = "df_session"


@dataclass
class User:
    id: str
    email: str
    name: str
    is_admin: bool
    csrf: str | None = None          # set for cookie sessions only


def _resolve(request: Request) -> User | None:
    header = request.headers.get("authorization", "")
    with db.tx() as conn:
        if header.lower().startswith("bearer "):
            r = db.one(conn, """SELECT u.* FROM api_tokens t JOIN users u ON u.id = t.user_id
                                WHERE t.token_hash = %s AND t.revoked_at IS NULL""", (digest(header[7:].strip()),))
            if not r:
                raise unauthorized("invalid API token", "invalid_token")
            return User(r["id"], r["email"], r["name"], r["is_admin"])
        raw = request.cookies.get(COOKIE)
        if not raw:
            return None
        r = db.one(conn, """SELECT u.*, s.csrf FROM sessions s JOIN users u ON u.id = s.user_id
                            WHERE s.token_hash = %s AND s.expires_at > now()""", (digest(raw),))
        return User(r["id"], r["email"], r["name"], r["is_admin"], r["csrf"]) if r else None


def user(request: Request) -> User | None:
    if not hasattr(request.state, "user"):
        request.state.user = _resolve(request)
    return request.state.user


def need_user(request: Request) -> User:
    u = user(request)
    if u is None:
        raise unauthorized()
    return u


async def guard(request: Request) -> None:
    """App-wide dependency: validate any bearer token; enforce CSRF on cookie-authenticated writes."""
    u = user(request)
    if request.method in ("GET", "HEAD", "OPTIONS") or u is None or u.csrf is None:
        return
    sent = request.headers.get("x-csrf-token")
    if sent is None and request.headers.get("content-type", "").startswith(("application/x-www-form", "multipart/")):
        sent = (await request.form()).get("csrf")
    if sent != u.csrf:
        raise forbidden("form expired, reload the page", "csrf_failed")


def login(response: Response, user_id: str) -> None:
    raw = new_token(32)
    with db.tx() as conn:
        conn.execute("INSERT INTO sessions VALUES (%s, %s, %s, %s)",
                     (digest(raw), user_id, new_token(16), datetime.now(timezone.utc) + timedelta(days=14)))
    response.set_cookie(COOKIE, raw, max_age=14 * 86400, httponly=True, samesite="lax",
                        secure=config.SECURE_COOKIES, path="/")


def logout(request: Request, response: Response) -> None:
    if raw := request.cookies.get(COOKIE):
        with db.tx() as conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = %s", (digest(raw),))
    response.delete_cookie(COOKIE, path="/")
