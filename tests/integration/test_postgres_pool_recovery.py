"""The connection pool, with the application's own settings, on PostgreSQL.

app/core/database.py leans on pool_pre_ping and the server-side
idle_in_transaction_session_timeout to keep the pool healthy, but the suite
runs on NullPool engines and only compared those settings as strings. Here the
pool is the real one (tests/integration/pg_pool_support.py) and the server
is made to misbehave: every other backend is terminated between two requests.

Skipped unless TEST_DATABASE_URL names a server; CI's test-postgres job runs it.
"""

import pytest
from conftest import TEST_DB_URL, requires_postgres
from pg_pool_support import APPLICATION_NAME, app_pool
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from app.core.config import settings

pytestmark = requires_postgres

PRODUCTS = "/api/v1/products"
USERS = "/api/v1/users"


@pytest.fixture
def admin():
    """A connection of its own, outside the pool under test."""
    engine = create_engine(TEST_DB_URL, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        yield connection
    engine.dispose()


def _pool_backends(admin):
    rows = admin.execute(
        text("SELECT pid FROM pg_stat_activity WHERE datname = current_database() AND application_name = :name"),
        {"name": APPLICATION_NAME},
    )
    return {pid for (pid,) in rows}


def _terminate_every_other_backend(admin):
    # With a timeout the call waits until the backend is gone, so the next
    # request cannot race a backend that is still dying.
    admin.execute(
        text(
            "SELECT pg_terminate_backend(pid, 5000) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()"
        )
    ).fetchall()


def _idle_in_transaction(admin):
    return admin.execute(
        text(
            "SELECT pid, state, left(query, 80) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid() "
            "AND state LIKE 'idle in transaction%'"
        )
    ).fetchall()


def test_the_app_pool_has_pre_ping_on(client, monkeypatch):
    # The premise of the tests below: this is what a deployment runs with.
    assert settings.DB_POOL_PRE_PING is True
    with app_pool(monkeypatch, client) as engine:
        assert engine.pool._pre_ping is True


def test_the_next_request_succeeds_after_the_server_killed_every_pooled_connection(client, monkeypatch, admin):
    with app_pool(monkeypatch, client) as engine:
        assert client.get(PRODUCTS).status_code == 200
        before = _pool_backends(admin)
        assert before, "the first request should have left a pooled connection"

        _terminate_every_other_backend(admin)
        assert not _pool_backends(admin)

        response = client.get(PRODUCTS)

        assert response.status_code == 200
        after = _pool_backends(admin)
        assert after and not (after & before)  # a new backend, not the dead one
        assert engine.pool.checkedout() == 0


def test_without_pre_ping_the_same_kill_fails_one_request_and_the_pool_then_heals(client, monkeypatch, admin):
    # The control: shows that the test above is what pre_ping buys.
    with app_pool(monkeypatch, client, pre_ping=False) as engine:
        assert client.get(PRODUCTS).status_code == 200
        _terminate_every_other_backend(admin)

        failed = client.get(PRODUCTS)

        assert failed.status_code == 500
        assert failed.json()["detail"] == "A database error occurred"
        assert client.get(PRODUCTS).status_code == 200
        assert engine.pool.checkedout() == 0


def test_no_session_is_left_idle_in_transaction_after_requests(client, monkeypatch, admin, valid_registration_payload):
    with app_pool(monkeypatch, client) as engine:
        client.post(f"{USERS}/register", json=valid_registration_payload)
        tokens = client.post(
            f"{USERS}/login",
            data={"username": valid_registration_payload["email"], "password": valid_registration_payload["password"]},
        ).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        requests = [
            lambda: client.get(PRODUCTS),
            lambda: client.get(f"{PRODUCTS}/999999"),  # 404
            lambda: client.get(f"{PRODUCTS}?limit=-1"),  # 422
            lambda: client.get(f"{USERS}/me", headers=headers),  # the auth dependency reads, then ends its transaction
            lambda: client.get(f"{USERS}/orders", headers=headers),
            lambda: client.post(f"{USERS}/logout", headers=headers),
        ]
        for send in requests:
            response = send()
            assert response.status_code < 500, response.text
            assert _idle_in_transaction(admin) == []
            assert engine.pool.checkedout() == 0
