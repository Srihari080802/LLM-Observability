"""
db.py
=====

Thin async PostgreSQL helper built on asyncpg.

We use a CONNECTION POOL rather than a connection-per-request because:
    * opening a Postgres connection is expensive (~ms of TCP + auth)
    * a pool caps the total connections so a traffic spike can't
      exhaust the database's connection slots
The pool is created on app startup and closed on shutdown.
"""
from __future__ import annotations
import os
import asyncpg

_pool: asyncpg.Pool | None = None


def _dsn() -> str:
    """Build the Postgres connection string from environment variables."""
    return (
        f"postgresql://{os.environ.get('DB_USER', 'llm')}:"
        f"{os.environ.get('DB_PASSWORD', 'llm')}@"
        f"{os.environ.get('DB_HOST', 'localhost')}:"
        f"{os.environ.get('DB_PORT', '5432')}/"
        f"{os.environ.get('DB_NAME', 'llm_obs')}"
    )


async def init_pool() -> None:
    """Create the global connection pool. Call once on startup."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(_dsn(), min_size=2, max_size=10)


async def close_pool() -> None:
    """Close the pool. Call once on shutdown."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    """Return the live pool, or raise if it was never initialised."""
    if _pool is None:
        raise RuntimeError("DB pool not initialised — call init_pool() first")
    return _pool
