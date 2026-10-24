"""A burst of concurrent `login`/`reset_password` requests, each paying a
real (simulated) bcrypt cost, must not exhaust or leak the DB connection
pool, and no request may hold a connection while its password is hashed.

History (the production incident this guards against): login() and
reset_password() used to query the user - checking a connection out of the
pool and opening a transaction - and then awaited the CPU-bound bcrypt call
*while still holding that connection*. bcrypt runs in a shared threadpool,
so under load the connection stayed checked out for the queueing delay of
every other concurrent hash, not just its own cost, and a burst of logins
exhausted pool_size + max_overflow. A client disconnect parked on that await
could also strand the connection.

The routes are now on the async engine (Depends(get_async_db)) and still
release the session with `await db.close()` before hashing. This test runs
the real get_async_db against a small bounded AsyncAdaptedQueuePool (conftest
otherwise uses NullPool, which can neither exhaust nor leak) and checks, on
that pool, (1) that the number of checked-out connections is zero while the
hash runs, (2) that a burst far above the pool's capacity completes, and
(3) that nothing is left checked out afterwards.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool

import app.core.database as database_module
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

    # Seed with a plain sync engine on the same file, closed before the app
    # opens its own connections.
    seed_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False, "timeout": 30}
    )
    Base.metadata.create_all(bind=seed_engine)
    seed_session = sessionmaker(bind=seed_engine, expire_on_commit=False)()
    seed_session.add(User(
        email="loadtest@example.com",
        hashed_password=get_password_hash("Str0ngPass"),
        full_name="Load Test User",
        gdpr_consent=True,
        privacy_policy_accepted=True,
    ))
    seed_session.commit()
    seed_session.close()
    seed_engine.dispose()

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        connect_args={"timeout": 30},
        poolclass=AsyncAdaptedQueuePool,
        pool_size=POOL_SIZE,
        max_overflow=MAX_OVERFLOW,
        # Fail fast rather than hang if the pool really is exhausted - a
        # long real-world pool_timeout would just make a broken test slow,
        # not pass.
        pool_timeout=5,
    )
    # get_async_db reads database_module.AsyncSessionLocal when it runs, so
    # replacing it puts the real dependency on the bounded pool.
    monkeypatch.setattr(
        database_module,
        "AsyncSessionLocal",
        async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )

    try:
        with TestClient(app) as client:
            try:
                yield client, engine
            finally:
                # The pooled connections belong to the client's event loop:
                # close them there, before it goes away.
                client.portal.call(engine.dispose)
    finally:
        pass


class _CheckedOutDuring:
    """Records engine.pool.checkedout() each time the (patched) bcrypt call
    runs, i.e. while a request is waiting for its hash."""

    def __init__(self, engine):
        self.engine = engine
        self.samples = []

    def sample(self):
        self.samples.append(self.engine.pool.checkedout())


def test_login_holds_no_connection_while_the_password_is_checked(
    bounded_pool_client, monkeypatch
):
    client, engine = bounded_pool_client

    import bcrypt as bcrypt_module

    seen = _CheckedOutDuring(engine)
    original_checkpw = bcrypt_module.checkpw

    def spying_checkpw(password, hashed):
        seen.sample()
        return original_checkpw(password, hashed)

    monkeypatch.setattr(bcrypt_module, "checkpw", spying_checkpw)

    response = client.post(
        "/api/v1/users/login",
        data={"username": "loadtest@example.com", "password": "Str0ngPass"},
    )

    assert response.status_code == 200
    # The user was read (a connection was used) and the hash was computed
    # after the session was closed: none was checked out at that moment.
    assert seen.samples == [0]
    assert engine.pool.checkedout() == 0


def test_password_reset_holds_no_connection_while_the_new_password_is_hashed(
    bounded_pool_client, monkeypatch
):
    client, engine = bounded_pool_client

    import bcrypt as bcrypt_module

    from app.core.security import generate_password_reset_token

    seen = _CheckedOutDuring(engine)
    original_hashpw = bcrypt_module.hashpw

    def spying_hashpw(password, salt):
        seen.sample()
        return original_hashpw(password, salt)

    seed_engine = create_engine(f"sqlite:///{engine.url.database}")
    with sessionmaker(bind=seed_engine)() as session:
        user = session.query(User).filter(User.email == "loadtest@example.com").one()
        token = generate_password_reset_token(user.email, user.hashed_password)
    seed_engine.dispose()

    monkeypatch.setattr(bcrypt_module, "hashpw", spying_hashpw)

    response = client.post(
        "/api/v1/users/password/reset/confirm",
        json={"token": token, "current_password": "Str0ngPass", "new_password": "NewStr0ngPass1"},
    )

    assert response.status_code == 200
    assert seen.samples == [0]
    assert engine.pool.checkedout() == 0


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

    seed_engine = create_engine(f"sqlite:///{engine.url.database}")
    with sessionmaker(bind=seed_engine)() as session:
        user = session.query(User).filter(User.email == "loadtest@example.com").one()
        token = generate_password_reset_token(user.email, user.hashed_password)
    seed_engine.dispose()

    n_requests = TOTAL_CAPACITY * 8

    def do_reset():
        return client.post(
            "/api/v1/users/password/reset/confirm",
            json={"token": token, "current_password": "Str0ngPass", "new_password": "NewStr0ngPass1"},
        )

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=n_requests) as executor:
        futures = [executor.submit(do_reset) for _ in range(n_requests)]
        responses = [f.result(timeout=30) for f in futures]
    burst_elapsed = time.monotonic() - started

    # All requests present the same valid token, and a reset token is
    # single-use: exactly one of them resets the password and the rest are
    # told the token is spent. Each call still must complete without a
    # pool-timeout-driven 500.
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200] + [400] * (n_requests - 1), [
        (r.status_code, r.text) for r in responses if r.status_code not in (200, 400)
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
