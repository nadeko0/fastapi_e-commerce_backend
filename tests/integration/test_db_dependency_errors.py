"""One session dependency, one behaviour on a database error.

The legal router used to depend on app.api.deps.get_db, which only closed the
session, while every other route used the rolling-back dependency with the
generic 500. A database error in a legal route therefore escaped as an
unhandled exception. The legal routes now depend on database.get_async_db,
which rolls back and answers a generic 500 without the SQL; app.api.deps.get_db
is gone, and with it the test of its failing-SessionLocal() behaviour.
"""

from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.core.database as database_module
from app.main import app

USERS_PREFIX = "/api/v1/users"
LEGAL_PREFIX = "/api/v1/legal"

SECRET_SQL = "SELECT users.hashed_password FROM users WHERE users.email = 'leak@example.com'"


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
    ).json()["data"]["access_token"]

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
    reference_app = FastAPI()

    @reference_app.get("/reference")
    async def reference(db: Annotated[Any, Depends(database_module.get_async_db)]):
        _db_error()

    with TestClient(reference_app, raise_server_exceptions=False) as lenient:
        reference_response = lenient.get("/reference")

    assert reference_response.status_code == 500
    assert reference_response.json() == {"detail": "A database error occurred"}
    assert legal.status_code == reference_response.status_code
    assert legal.json() == reference_response.json()
    assert "SELECT" not in legal.text and "leak@example.com" not in legal.text
