"""PostgreSQL access: a small connection pool and the schema bootstrap."""
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None


def init(database_url: str, max_size: int = 10) -> None:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(database_url, min_size=1, max_size=max_size,
                               kwargs={"row_factory": dict_row}, open=True)
        _pool.wait(timeout=60)


def close() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx():
    """One transaction: commits on success, rolls back on error."""
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    with _pool.connection() as conn:
        with conn.transaction():
            yield conn


def apply_schema() -> None:
    sql = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
    with tx() as conn:
        # serialise schema changes when the app and the worker start together
        conn.execute("SELECT pg_advisory_xact_lock(4711)")
        conn.execute(sql)
