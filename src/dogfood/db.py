"""Database access: a connection pool, a transaction helper and a small migration runner.

Queries are plain SQL. The schema lives in migrations/*.sql and is applied in file order.
"""

import logging
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from . import config

log = logging.getLogger("dogfood.db")
_pool = None


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(config.DATABASE_URL, min_size=1, max_size=10, open=True,
                               kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"})
    return _pool


@contextmanager
def tx():
    """One transaction: commit on success, roll back on any exception."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def one(conn, sql, args=None):
    return conn.execute(sql, args).fetchone()


def rows(conn, sql, args=None):
    return conn.execute(sql, args).fetchall()


def val(conn, sql, args=None):
    r = conn.execute(sql, args).fetchone()
    return None if r is None else next(iter(r.values()))


def jsonb(v):
    return Jsonb(v)


def migrate() -> None:
    """Apply migrations/NNN_*.sql not yet recorded. An advisory lock makes concurrent boots safe."""
    with pool().connection() as conn:
        conn.execute("SELECT pg_advisory_lock(4201)")
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, "
                         "applied_at timestamptz NOT NULL DEFAULT now())")
            conn.commit()
            done = {r["version"] for r in conn.execute("SELECT version FROM schema_migrations")}
            for f in sorted((Path(__file__).parent / "migrations").glob("*.sql")):
                if f.stem not in done:
                    with conn.transaction():
                        conn.execute(f.read_text())
                        conn.execute("INSERT INTO schema_migrations VALUES (%s)", (f.stem,))
                    log.info("migration %s applied", f.stem)
        finally:
            conn.execute("SELECT pg_advisory_unlock(4201)")
            conn.commit()
