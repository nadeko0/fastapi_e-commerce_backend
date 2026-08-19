"""Proves the fix for the production incident: a burst of concurrent
`login`/`reset_password` requests, each paying a real (simulated) bcrypt
cost, must not exhaust or permanently leak the DB connection pool.

Root cause (confirmed, not guessed): login() and reset_password() used to
query the user - checking a connection out of the pool and opening an
implicit transaction - and then `await` the CPU-bound bcrypt call *while
still holding that connection*. verify_password_async/get_password_hash_async
offload to anyio's shared threadpool (see app/core/security.py), which also
carries every other concurrent request's bcrypt work and FastAPI's own
threadpool dispatch of sync `Depends(get_db)` dependencies. Under load that
shared queue backs up, so a connection held open across the await is held
for the cumulative queueing delay of every other concurrent bcrypt call, not
just its own hash cost - which is what let a burst of concurrent
logins/registrations exhaust pool_size + max_overflow.

Confirmed separately (see the instrumentation in this investigation) that a
client disconnect while a request is parked on that await can strand the
connection *permanently*: FastAPI's dependency cleanup itself runs via
another threadpool `await`, and anyio delivers cancellation per cancel-scope
- once a scope is cancelled, any further checkpoint inside it (including the
cleanup call) raises immediately instead of running, so `session.close()`
never executes. That matches the reported symptom of connections stuck
`idle in transaction` in `pg_stat_activity` even after all client traffic
had stopped.

The fix (see login()/reset_password() in app/api/v1/users.py) releases the
DB session's connection *before* awaiting the CPU-bound bcrypt call, so
there is nothing left open for either queueing delay or a disconnect to
hold/strand.

conftest.py's default dependency_overrides point every session at a single
StaticPool in-memory SQLite connection, which never actually enforces a
connection limit and so can't reproduce (or disprove) pool exhaustion. This
test instead swaps app.core.database's module-level engine/SessionLocal for
a real, small QueuePool-backed SQLite engine and lets the *real*
get_db/session_scope code run against it.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import QueuePool

import app.core.database as database_module
from app.core.database import get_db as database_get_db
from app.core.security import get_password_hash
from app.main import app
from app.models.base import Base
from app.models.user import User

POOL_SIZE = 2
MAX_OVERFLOW = 2
TOTAL_CAPACITY = POOL_SIZE + MAX_OVERFLOW


@pytest.fixture
def bounded_pool_client(tmp_path, monkeypatch, fake_redis):
    db_path = tmp_path / "pool_test.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False, "timeout": 30},
        poolclass=QueuePool,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        # Fail fast rather than hang if the pool really is exhausted - a
        # long real-world pool_timeout would just make a broken test slow,
        # not pass.
        pool_timeout=5,
    )
    TestSessionLocal = sessionmaker(
        autocommit=False, autoflush=False, bind=engine, expire_on_commit=False
    )
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module, "SessionLocal", TestSessionLocal)

    # Remove conftest's blanket StaticPool override for this dependency so
    # the real get_db/session_scope code path - the thing under test - runs
    # against our bounded pool instead.
    removed_override = app.dependency_overrides.pop(database_get_db, None)

    seed_session = TestSessionLocal()
    seed_session.add(User(
        email="loadtest@example.com",
        hashed_password=get_password_hash("Str0ngPass"),
        full_name="Load Test User",
        gdpr_consent=True,
        privacy_policy_accepted=True,
    ))
    seed_session.commit()
    seed_session.close()

    try:
        with TestClient(app) as client:
            yield client, engine
    finally:
        if removed_override is not None:
            app.dependency_overrides[database_get_db] = removed_override
        engine.dispose()


def test_concurrent_logins_do_not_exhaust_or_leak_the_connection_pool(
    bounded_pool_client, monkeypatch
):
    client, engine = bounded_pool_client

    import bcrypt as bcrypt_module

    delay = 0.15
    original_checkpw = bcrypt_module.checkpw

    def slow_checkpw(password, hashed):
        time.sleep(delay)
        return original_checkpw(password, hashed)

    monkeypatch.setattr(bcrypt_module, "checkpw", slow_checkpw)

    # Well beyond pool_size + max_overflow, so the old (buggy) code - which
    # held a connection open for the entire queued bcrypt wait - could not
    # possibly serve them all without pool-timeout failures.
    n_requests = TOTAL_CAPACITY * 8

    def do_login():
        return client.post(
            "/api/v1/users/login",
            data={"username": "loadtest@example.com", "password": "Str0ngPass"},
        )

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=n_requests) as executor:
        futures = [executor.submit(do_login) for _ in range(n_requests)]
        responses = [f.result(timeout=30) for f in futures]
    burst_elapsed = time.monotonic() - started

    statuses = [r.status_code for r in responses]
    assert statuses.count(200) == n_requests, (
        f"expected all {n_requests} concurrent logins to succeed, got {statuses}"
    )

    # With the connection released before the bcrypt await, a burst this
    # size (8x pool capacity) should clear in a couple of queueing rounds
    # through the bcrypt semaphore, not scale with how long connections sit
    # around waiting on it. This is the concrete, measured difference from
    # the bug: holding the connection across the await made hold time (and
    # so this burst's total wall time) balloon with queueing depth instead
    # of staying close to a fixed number of bcrypt rounds.
    assert burst_elapsed < 6.0, (
        f"burst of {n_requests} logins took {burst_elapsed:.2f}s - connections "
        "may still be held open across the bcrypt await"
    )

    # Every checked-out connection must have been returned - this is the
    # core assertion: checked-out count is back to zero once every request
    # has completed, i.e. nothing was leaked.
    assert engine.pool.checkedout() == 0, (
        f"{engine.pool.checkedout()} connections still checked out after the "
        "burst finished - the pool leaked connections"
    )

    # And the pool must not be left permanently wedged: a request made right
    # after the burst should get a connection promptly, not queue behind
    # stale, never-released connections.
    started = time.monotonic()
    response = do_login()
    elapsed = time.monotonic() - started
    assert response.status_code == 200
    assert elapsed < 2.0, (
        f"post-burst login took {elapsed:.2f}s - pool may still be exhausted"
    )


def test_concurrent_password_resets_do_not_exhaust_or_leak_the_connection_pool(
    bounded_pool_client, monkeypatch
):
    """Same proof, for reset_password() - the other endpoint that queried
    the user before awaiting a CPU-bound bcrypt call (get_password_hash_async
    this time, not verify_password_async)."""
    client, engine = bounded_pool_client

    import bcrypt as bcrypt_module

    delay = 0.15
    original_hashpw = bcrypt_module.hashpw

    def slow_hashpw(password, salt):
        time.sleep(delay)
        return original_hashpw(password, salt)

    monkeypatch.setattr(bcrypt_module, "hashpw", slow_hashpw)

    from app.core.security import generate_password_reset_token

    token = generate_password_reset_token("loadtest@example.com")

    n_requests = TOTAL_CAPACITY * 8

    def do_reset():
        return client.post(
            f"/api/v1/users/password/reset/{token}",
            json={"current_password": "Str0ngPass", "new_password": "NewStr0ngPass1"},
        )

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=n_requests) as executor:
        futures = [executor.submit(do_reset) for _ in range(n_requests)]
        responses = [f.result(timeout=30) for f in futures]
    burst_elapsed = time.monotonic() - started

    # All requests reset the same user's password with a valid token - each
    # individual call still must complete without a pool-timeout-driven 500,
    # regardless of what the final password ends up being.
    assert all(r.status_code == 200 for r in responses), [
        (r.status_code, r.text) for r in responses if r.status_code != 200
    ]

    assert engine.pool.checkedout() == 0, (
        f"{engine.pool.checkedout()} connections still checked out after the "
        "burst finished - the pool leaked connections"
    )

    # Same reasoning as the login burst above: hold time (and so total wall
    # time) should stay close to a fixed number of bcrypt-hash rounds
    # instead of scaling with queueing depth.
    assert burst_elapsed < 6.0, (
        f"burst of {n_requests} password resets took {burst_elapsed:.2f}s - "
        "connections may still be held open across the bcrypt await"
    )
