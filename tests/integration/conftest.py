"""Live-service fixtures for the integration tree.

Integration tests assert behavior over real wire protocols — real PostgreSQL
transactions and real Redis Stream semantics — so these fixtures talk to the
services from ``docker/docker-compose.yml`` and nothing is faked.

Two deliberate design choices:

1. **A missing service aborts the run; it never skips.** A skipped integration
   test is indistinguishable from a passing one, which is precisely the false
   green this suite exists to prevent. The failure carries the command that
   fixes it.
2. **A dedicated database and Redis index, truncated per test.** Integration
   tests write real rows and real stream entries; leaking either into the next
   test (or into developer data) would make results order-dependent.
"""

from __future__ import annotations

import os
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
import redis as redis_sync
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import vouch.db.models  # noqa: F401  — import registers tables on Base.metadata
from vouch.db.base import Base
from vouch.queue.stream import EventStream

POSTGRES_HOST = os.environ.get("VOUCH_TEST_PG_HOST", "127.0.0.1")
POSTGRES_PORT = int(os.environ.get("VOUCH_TEST_PG_PORT", "5432"))
POSTGRES_USER = os.environ.get("VOUCH_TEST_PG_USER", "postgres")
POSTGRES_PASSWORD = os.environ.get("VOUCH_TEST_PG_PASSWORD", "mypassword")
ADMIN_DATABASE = "postgres"
TEST_DATABASE = os.environ.get("VOUCH_TEST_PG_DB", "vouch_test")
REDIS_URL = os.environ.get("VOUCH_TEST_REDIS_URL", "redis://127.0.0.1:6379/15")

SERVICES_REQUIRED = (
    "Integration tests require PostgreSQL on 5432 and Redis on 6379 "
    "(start with docker compose up -d)"
)

# TRUNCATE resets these together (CASCADE covers the FK from branch_jobs to
# investigations); per-table DELETE would leave the same result but is several
# round trips and does not reset identity columns.
ALL_TABLES = ("outbox_events", "submissions", "investigations", "branch_jobs")


def dsn(database: str) -> str:
    """SQLAlchemy DSN (async driver)."""
    return (
        f"postgresql+psycopg://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{database}"
    )


def psycopg_dsn(database: str) -> str:
    """Plain DSN: psycopg's own connect() rejects the +psycopg dialect suffix."""
    return (
        f"postgresql://{POSTGRES_USER}:{POSTGRES_PASSWORD}"
        f"@{POSTGRES_HOST}:{POSTGRES_PORT}/{database}"
    )


def _service_failure() -> str | None:
    """Return why the services are unusable, or None when both answer."""
    try:
        with psycopg.connect(psycopg_dsn(ADMIN_DATABASE), connect_timeout=3) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        return f"PostgreSQL unavailable: {exc}"

    try:
        client = redis_sync.Redis.from_url(
            REDIS_URL, socket_connect_timeout=3, socket_timeout=3
        )
        try:
            if not client.ping():
                return "Redis unavailable: PING returned False"
        finally:
            client.close()
    except Exception as exc:
        return f"Redis unavailable: {exc}"

    return None


def _ensure_test_database() -> None:
    with psycopg.connect(psycopg_dsn(ADMIN_DATABASE), autocommit=True, connect_timeout=3) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DATABASE,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DATABASE}"')


def _reset_schema() -> None:
    """Drop and recreate `public` so every run starts from the ORM models.

    Alembic currently carries a migration for `submissions` only, so migrations
    cannot build this schema. Without the reset, a table created by an earlier
    run keeps its old columns and the suite tests a schema that no longer
    exists in code — exactly the drift these tests exist to catch.
    """
    with psycopg.connect(
        psycopg_dsn(TEST_DATABASE), autocommit=True, connect_timeout=3
    ) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(scope="session", autouse=True)
def live_services():
    """Fail the whole integration run — loudly — when the services are absent."""
    reason = _service_failure()
    if reason:
        pytest.exit(f"{SERVICES_REQUIRED} [{reason}]")
    _ensure_test_database()
    _reset_schema()


@pytest_asyncio.fixture
async def engine(live_services):
    """Async engine bound to the dedicated test database, schema created.

    Created per test on purpose: pytest-asyncio runs this suite with a
    function-scoped event loop, and an engine created under one loop cannot be
    used from another.
    """
    engine = create_async_engine(dsn(TEST_DATABASE), pool_size=4, max_overflow=0)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def db_sessionmaker(engine):
    return async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session(db_sessionmaker):
    async with db_sessionmaker() as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def _isolate_database(engine):
    """Empty the tables after every integration test.

    Autouse on purpose: tying cleanup to a fixture that tests request only
    cleans up for the tests that happen to ask for it. A test that writes
    through the app (e.g. "the HTTP path never publishes") without holding a
    session would otherwise leak rows into the next test and make the suite
    order-dependent.
    """
    yield
    async with engine.begin() as conn:
        await conn.execute(
            text(f"TRUNCATE TABLE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
        )


@pytest_asyncio.fixture
async def redis_client() -> Redis:
    """Redis on the dedicated index, flushed before and after every test."""
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.flushdb()
        await client.aclose()


@pytest_asyncio.fixture
async def event_stream(redis_client) -> EventStream:
    """Real EventStream on the dedicated index, unique stream name per test."""
    stream = EventStream(redis_url=REDIS_URL, stream_name=f"test:events:{uuid4().hex}")
    try:
        yield stream
    finally:
        await stream.close()