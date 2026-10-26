"""One session dependency, one behaviour on a database error.

A database that cannot be reached (connection refused, lost, pool checkout timeout,
connection invalidated) answers RFC 9110 15.6.4 503 Service Unavailable with
Retry-After: 5 and RFC 9457 problem+json, without leaking SQL or parameters.
A genuine server bug (IntegrityError, ProgrammingError escaping a route) stays 500.
"""

import socket
import sys
from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import (
    DBAPIError,
    DisconnectionError,
    IntegrityError,
    InterfaceError,
    OperationalError,
    ProgrammingError,
)
from sqlalchemy.exc import (
    TimeoutError as SATimeoutError,
)
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.core.database as database_module
from app.core.config import settings
from app.core.errors import PROBLEM_MEDIA_TYPE, http_exception_handler
from app.main import app
from app.services.payment import StripePaymentProvider

USERS_PREFIX = "/api/v1/users"
LEGAL_PREFIX = "/api/v1/legal"

SECRET_SQL = "SELECT users.hashed_password FROM users WHERE users.email = 'leak@example.com'"


def _make_test_app() -> FastAPI:
    test_app = FastAPI()
    test_app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    return test_app


def _db_error(*_args, **_kwargs):
    raise OperationalError(SECRET_SQL, {"email": "leak@example.com"}, Exception("db is down"))


def test_database_error_in_a_legal_route_answers_like_any_other_async_route(client, monkeypatch):
    payload = {
        "email": "legal.user@example.com",
        "password": "Str0ngPass1",
        "full_name": "Legal User",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }
    client.post(f"{USERS_PREFIX}/register", json=payload)
    token = client.post(
        f"{USERS_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    ).json()["access_token"]

    async def failing_load_user(*_args, **_kwargs):
        _db_error()

    monkeypatch.setattr("app.api.v1.legal._load_user", failing_load_user)
    with TestClient(app, raise_server_exceptions=False) as lenient:
        legal = lenient.post(
            f"{LEGAL_PREFIX}/consent",
            json={"marketing_consent": True,
                  "privacy_policy_accepted": True},
            headers={"Authorization": f"Bearer {token}"},
        )

    # The reference: a route on database.get_async_db failing the same way.
    reference_app = _make_test_app()

    @reference_app.get("/reference")
    async def reference(db: Annotated[Any, Depends(database_module.get_async_db)]):
        _db_error()

    with TestClient(reference_app, raise_server_exceptions=False) as lenient:
        reference_response = lenient.get("/reference")

    assert reference_response.status_code == 503
    assert reference_response.headers.get("Retry-After") == "5"
    assert reference_response.headers.get("content-type") == PROBLEM_MEDIA_TYPE
    assert reference_response.json()["detail"] == "The database is temporarily unavailable, please retry"
    assert reference_response.json()["retry_after"] == 5
    assert legal.status_code == 503
    assert legal.headers.get("Retry-After") == "5"
    assert legal.json()["detail"] == reference_response.json()["detail"]
    assert "SELECT" not in legal.text and "leak@example.com" not in legal.text


def test_route_db_operational_error_connection_refused_is_503():
    test_app = _make_test_app()

    @test_app.get("/test-db")
    async def test_endpoint(db: Annotated[Any, Depends(database_module.get_async_db)]):
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/test-db")

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["type"] == "about:blank"
    assert body["title"] == "Service Unavailable"
    assert body["status"] == 503
    assert body["detail"] == "The database is temporarily unavailable, please retry"
    assert body["retry_after"] == 5


def test_route_db_interface_error_is_503():
    test_app = _make_test_app()

    @test_app.get("/test-db")
    async def test_endpoint(db: Annotated[Any, Depends(database_module.get_async_db)]):
        raise InterfaceError("SELECT 1", {}, Exception("connection already closed"))

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/test-db")

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["detail"] == "The database is temporarily unavailable, please retry"
    assert body["retry_after"] == 5


def test_route_db_pool_timeout_is_503():
    test_app = _make_test_app()

    @test_app.get("/test-db")
    async def test_endpoint(db: Annotated[Any, Depends(database_module.get_async_db)]):
        raise SATimeoutError("QueuePool limit of size 10 overflow 5 reached, connection timed out")

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/test-db")

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["detail"] == "The database is temporarily unavailable, please retry"
    assert body["retry_after"] == 5


def test_route_dbapi_error_connection_invalidated_is_503():
    test_app = _make_test_app()

    @test_app.get("/test-db")
    async def test_endpoint(db: Annotated[Any, Depends(database_module.get_async_db)]):
        err = DBAPIError("SELECT 1", {}, Exception("server restart"))
        err.connection_invalidated = True
        raise err

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/test-db")

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["detail"] == "The database is temporarily unavailable, please retry"
    assert body["retry_after"] == 5


def test_route_disconnection_error_is_503():
    test_app = _make_test_app()

    @test_app.get("/test-db")
    async def test_endpoint(db: Annotated[Any, Depends(database_module.get_async_db)]):
        raise DisconnectionError("connection disconnected")

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/test-db")

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["detail"] == "The database is temporarily unavailable, please retry"
    assert body["retry_after"] == 5


def test_route_integrity_error_is_500_without_details():
    test_app = _make_test_app()

    @test_app.get("/test-db")
    async def test_endpoint(db: Annotated[Any, Depends(database_module.get_async_db)]):
        raise IntegrityError(
            "INSERT INTO users (email) VALUES (:email)",
            {"email": "secret@example.com"},
            Exception("unique constraint violation"),
        )

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/test-db")

    assert response.status_code == 500
    assert "Retry-After" not in response.headers
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["status"] == 500
    assert "secret@example.com" not in response.text
    assert "INSERT INTO" not in response.text


def test_route_programming_error_is_500_without_details():
    test_app = _make_test_app()

    @test_app.get("/test-db")
    async def test_endpoint(db: Annotated[Any, Depends(database_module.get_async_db)]):
        raise ProgrammingError(
            "SELECT secret_column FROM non_existent_table",
            {},
            Exception("syntax error"),
        )

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/test-db")

    assert response.status_code == 500
    assert "Retry-After" not in response.headers
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["status"] == 500
    assert "secret_column" not in response.text


def test_stripe_webhook_under_database_outage_answers_503(client, monkeypatch):
    import app.services.stripe_events as stripe_events

    def _database_down(*args, **kwargs):
        raise OperationalError("INSERT INTO webhook_events", {}, Exception("connection lost"))

    monkeypatch.setattr(stripe_events, "store_event", _database_down)

    payload = b'{"id": "evt_test_db_outage", "type": "payment_intent.succeeded", "created": 1234567890, "data": {}}'
    sig = StripePaymentProvider.sign_payload(
        payload, settings.STRIPE_WEBHOOK_SECRET.get_secret_value()
    )

    with TestClient(app, raise_server_exceptions=False) as lenient:
        response = lenient.post(
            "/api/v1/orders/webhooks/stripe",
            content=payload,
            headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
        )

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE
    body = response.json()
    assert body["detail"] == "The database is temporarily unavailable, please retry"
    assert body["retry_after"] == 5


def test_closed_port_database_engine_answers_503(monkeypatch):
    if sys.platform == "win32":
        import asyncio
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Find an unused closed port on localhost
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    closed_port = sock.getsockname()[1]
    sock.close()

    engine = create_async_engine(
        f"postgresql+psycopg://postgres:secret@127.0.0.1:{closed_port}/nonexistent?connect_timeout=1",
        pool_timeout=1,
    )
    closed_sessionmaker = async_sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(database_module, "AsyncSessionLocal", closed_sessionmaker)

    test_app = _make_test_app()

    @test_app.get("/query")
    async def query(db: Annotated[Any, Depends(database_module.get_async_db)]):
        await db.execute(text("SELECT 1"))

    with TestClient(test_app, raise_server_exceptions=False) as client:
        response = client.get("/query")

    assert response.status_code == 503
    assert response.headers.get("Retry-After") == "5"
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE
    assert response.json()["detail"] == "The database is temporarily unavailable, please retry"
