"""Tests run the real app on a real port against a real Postgres (TEST_DATABASE_URL), seeded from
fixtures.json exactly like `docker compose up`. In Docker: `docker compose run --rm app pytest`."""

import os
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost/dogfood_test")
os.environ.setdefault("DOGFOOD_FIXTURES", str(ROOT / "fixtures.json"))
os.environ["DOGFOOD_DEMO"] = "true"

s = socket.socket(); s.bind(("127.0.0.1", 0)); PORT = s.getsockname()[1]; s.close()
BASE = f"http://127.0.0.1:{PORT}"

ORGANIZER, JUDGE_A, JUDGE_B, PARTICIPANT, ADMIN = (
    "df_organizer_demo", "df_judge_a_demo", "df_judge_b_demo", "df_participant_demo", "df_admin_demo")


@pytest.fixture(scope="session")
def server():
    import psycopg
    import uvicorn

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        c.execute("DROP SCHEMA public CASCADE")
        c.execute("CREATE SCHEMA public")
    from dogfood.app import app
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(200):
        if srv.started:
            break
        time.sleep(0.05)
    yield BASE
    srv.should_exit = True


@pytest.fixture
def api(server):
    import httpx

    def make(token=None):
        return httpx.Client(base_url=server, headers={"Authorization": f"Bearer {token}"} if token else {},
                            follow_redirects=False, timeout=15)
    return make


@pytest.fixture
def sql(server):
    from dogfood import db

    def run(q, args=None):
        with db.tx() as conn:
            cur = conn.execute(q, args)
            return cur.fetchall() if cur.description else cur.rowcount
    return run
