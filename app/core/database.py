import logging
from contextlib import contextmanager
from typing import AsyncGenerator, Generator

from fastapi import HTTPException, status
from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

logger = logging.getLogger(__name__)

# Defense-in-depth against connection-pool leaks (see the long comments in
# login()/reset_password() in app/api/v1/users.py and register_user() for the
# app-level fixes): even with every known code path fixed to release its
# connection before an await, FastAPI's own generator-dependency teardown
# (fastapi.concurrency.contextmanager_in_threadpool) has no `finally` around
# its yield - a cancellation delivered while a request is suspended anywhere
# in that yielded region skips `cm.__exit__` (i.e. `session.close()` in
# get_db) entirely, with no exception raised and nothing left to catch. That
# is a property of FastAPI/anyio's cancellation model, not a bug in any one
# route, so no amount of route-level "close before the await" patching can
# make it impossible in general - only less likely by shrinking the window.
#
# Postgres itself can bound the damage: `idle_in_transaction_session_timeout`
# forcibly terminates any connection that sits idle inside an open
# transaction for longer than the timeout, at the server, regardless of
# whether the client-side app code ever gets a chance to run its own
# cleanup. Combined with DB_POOL_PRE_PING (settings.DB_POOL_PRE_PING,
# default True - see app/core/config.py), SQLAlchemy validates each
# connection with a cheap "SELECT 1" on checkout and transparently discards
# a connection Postgres has since killed rather than erroring, so a leaked
# connection self-heals within one checkout cycle after the timeout elapses
# instead of staying wedged (permanently exhausting the pool) until a manual
# restart. `statement_timeout` is set too, as the same kind of backstop for
# a single query/statement that never returns.
#
# SQLite (used by the whole test suite, and any other non-Postgres dialect)
# has no such session-level option, so this only applies when the configured
# DATABASE_URI is actually a Postgres URL - passing `options=...` connect
# args to sqlite3 would fail outright rather than being ignored.
def _build_connect_args(database_uri: str) -> dict:
    if database_uri.startswith(("postgresql", "postgres")):
        return {
            "options": (
                "-c idle_in_transaction_session_timeout=30000 "
                "-c statement_timeout=60000"
            )
        }
    return {}


_connect_args = _build_connect_args(settings.DATABASE_URI)

_engine_kwargs = dict(
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_timeout=settings.DB_POOL_TIMEOUT,
    pool_pre_ping=settings.DB_POOL_PRE_PING,
    pool_recycle=3600,
    connect_args=_connect_args,
    echo=False,
    echo_pool=False,
    logging_name=None
)

engine = create_engine(settings.DATABASE_URI, **_engine_kwargs)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    expire_on_commit=False
)

# The async engine, for routes that have been moved off the sync Session.
#
# Every route used to be `async def` calling the sync Session above inline,
# which runs each query on the event loop thread. Under load that does worse
# than serialize requests: a request keeps its pooled connection until
# get_db's teardown, the event loop is what dispatches that teardown, and so
# once every connection belongs to a finished-but-not-yet-torn-down request
# the next handler blocks the loop waiting for a connection that only the
# loop could have returned. The server stops answering entirely until
# pool_timeout fires. Measured and stack-dumped under locust - see README,
# "Dependency refresh and load test". With an AsyncSession, waiting for a
# connection (and for the query) yields to the loop instead of blocking it.
#
# Routes are being moved over one router at a time, so both engines exist
# for now, each with its own pool: a worker can hold up to
# 2 * (DB_POOL_SIZE + DB_MAX_OVERFLOW) Postgres connections until the sync
# engine is only used by Celery (app/tasks.py), Alembic, and scripts.
#
# psycopg 3 is the driver for both: `postgresql+psycopg://` selects its sync
# or async mode depending on which create_*engine() is given the URL, so
# there is no second driver and no second DATABASE_URI. A DATABASE_URI that
# names a sync-only driver (e.g. postgresql+psycopg2://) fails right here,
# at import, rather than on the first request to an async route.
async_engine = create_async_engine(settings.DATABASE_URI, **_engine_kwargs)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    autoflush=False,
    # Same as SessionLocal: objects stay readable after commit. Under asyncio
    # this is not optional - touching an expired attribute would trigger a
    # lazy load, which cannot run outside an awaited call and raises
    # MissingGreenlet instead.
    expire_on_commit=False,
)

@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except SQLAlchemyError as e:
        session.rollback()
        # str(e) on a SQLAlchemy error includes the failing SQL statement
        # and its bound parameter values by default - for an INSERT/UPDATE
        # that's frequently user-submitted data (email, address, etc).
        # Returning that verbatim in a 500 response leaks both PII and
        # internal schema/query details to the client; log it in full
        # server-side instead and return a generic message.
        logger.error("Database error in session_scope", exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="A database error occurred"
        )
    finally:
        session.close()

def get_db() -> Generator[Session, None, None]:
    with session_scope() as session:
        try:
            yield session
        except SQLAlchemyError as e:
            logger.error("Database error in get_db", exc_info=e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="A database error occurred"
            )


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    # Async counterpart of get_db: commit on success, roll back and answer
    # with a generic 500 on a database error (never the raw SQLAlchemy
    # message - see session_scope above for why). `async with` closes the
    # session in this same task, including when the request is cancelled, so
    # the "cancelled before teardown ran" leak described at the top of this
    # module has no equivalent here.
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except SQLAlchemyError as e:
            await session.rollback()
            logger.error("Database error in get_async_db", exc_info=e)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="A database error occurred"
            )
