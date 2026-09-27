"""Application wiring: startup, error rendering, security headers, routers."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import psycopg
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config, db, seed
from .auth import guard
from .errors import HTTPError
from .routes import api, integrations, judge, organize, people, public, vote
from .web import page, wants_json

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(app):
    db.migrate()
    seed.run()
    worker = None
    if config.WEBHOOK_WORKER:
        from .services.webhooks import Worker
        worker = Worker()
        worker.start()
    yield
    if worker:
        worker.stop.set()


app = FastAPI(title="DOGFOOD portal API", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
              openapi_url="/api/openapi.json", dependencies=[Depends(guard)])
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.include_router(public.router)
app.include_router(api.router)
app.include_router(people.router)
app.include_router(judge.router)
app.include_router(organize.router)
app.include_router(vote.router)
app.include_router(vote.api)
app.include_router(integrations.api)
app.include_router(integrations.router)


def _render(request: Request, status: int, code: str, message: str):
    if wants_json(request):
        return JSONResponse({"error": code, "message": message}, status_code=status)
    if status == 401 and request.method == "GET":
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return page(request, "error.html", status, status=status, code=code, message=message)


@app.exception_handler(HTTPError)
async def http_error(request: Request, e: HTTPError):
    return _render(request, e.status, e.code, e.message)


@app.exception_handler(psycopg.Error)
async def db_error(request: Request, e: psycopg.Error):
    """A database rule caught something: a clean 4xx, never a 500 with a stack trace."""
    state = e.sqlstate or ""
    if isinstance(e, psycopg.DataError):          # e.g. a NUL byte, which Postgres text cannot hold
        return _render(request, 400, "invalid_data", "that input contains characters that cannot be stored")
    if state == "DF001":
        return _render(request, 403, "submissions_closed", str(e.diag.message_primary))
    if state == "DF005":
        return _render(request, 403, "voting_closed", str(e.diag.message_primary))
    if state in ("DF003", "DF004", "DF006"):
        return _render(request, 409, "results_locked", str(e.diag.message_primary))
    if state.startswith(("23", "22", "DF")):   # integrity / data errors / our own triggers
        return _render(request, 400, "invalid_data", "that would break a data rule, so nothing was saved")
    raise e


@app.middleware("http")
async def no_nul_bytes(request: Request, call_next):
    """A NUL byte can never be stored in Postgres text; refuse it at the door instead of deep inside."""
    if "\x00" in request.url.path or "%00" in str(request.url.query) or "%00" in request.url.path:
        return JSONResponse({"error": "invalid_data", "message": "NUL bytes are not allowed"}, status_code=400)
    return await call_next(request)


@app.middleware("http")
async def headers(request: Request, call_next):
    resp = await call_next(request)
    embeddable = request.url.path.startswith("/embed/")      # the widget is the only page other sites may frame
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if not embeddable:
        resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                            "img-src 'self' data:; form-action 'self'; "
                            + ("frame-ancestors *; script-src 'none'" if embeddable else "frame-ancestors 'none'"))
    return resp


@app.get("/healthz", include_in_schema=False)
def healthz():
    with db.tx() as conn:
        db.val(conn, "SELECT 1")
    return {"ok": True}
