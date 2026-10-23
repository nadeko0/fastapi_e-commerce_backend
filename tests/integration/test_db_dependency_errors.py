"""One session dependency, one behaviour on a database error.

The legal router used to depend on app.api.deps.get_db, which only closed the
session, while every other sync route used app.core.database.get_db, which
rolls back and answers a generic 500 without the SQL. A database error in a
legal route therefore escaped as an unhandled exception, and deps.get_db hid
a failing SessionLocal() behind an UnboundLocalError.
"""

import pytest
from conftest import TestingSessionLocal
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.api.deps as deps
import app.core.database as database_module
from app.main import app

USERS_PREFIX = "/api/v1/users"
LEGAL_PREFIX = "/api/v1/legal"

SECRET_SQL = "SELECT users.hashed_password FROM users WHERE users.email = 'leak@example.com'"


def _db_error(*_args, **_kwargs):
    raise OperationalError(SECRET_SQL, {"email": "leak@example.com"}, Exception("db is down"))


def test_get_db_reports_the_real_error_when_the_session_cannot_be_created(monkeypatch):
    def broken_session_local():
        raise RuntimeError("cannot connect")

    monkeypatch.setattr(deps, "SessionLocal", broken_session_local)
    monkeypatch.setattr(database_module, "SessionLocal", broken_session_local)

    dependency = deps.get_db()
    with pytest.raises(RuntimeError, match="cannot connect"):
        next(dependency)


@pytest.fixture
def real_db_dependencies(monkeypatch):
    """Run the real session dependencies (conftest overrides them) against
    the test database."""
    monkeypatch.setattr(deps, "SessionLocal", TestingSessionLocal)
    monkeypatch.setattr(database_module, "SessionLocal", TestingSessionLocal)
    for dependency in (deps.get_db, database_module.get_db):
        monkeypatch.delitem(app.dependency_overrides, dependency, raising=False)


def test_database_error_in_a_legal_route_answers_like_any_other_sync_route(
    client, real_db_dependencies, monkeypatch
):
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

    monkeypatch.setattr("app.api.v1.legal._load_user", _db_error)
    with TestClient(app, raise_server_exceptions=False) as lenient:
        legal = lenient.post(
            f"{LEGAL_PREFIX}/consent",
            json={"marketing_consent": True,
                  "privacy_policy_accepted": True},
            headers={"Authorization": f"Bearer {token}"},
        )

    # The reference: a route on app.core.database.get_db failing the same way.
    reference_app = FastAPI()

    @reference_app.get("/reference")
    def reference(db=Depends(database_module.get_db)):
        _db_error()

    with TestClient(reference_app, raise_server_exceptions=False) as lenient:
        reference_response = lenient.get("/reference")

    assert reference_response.status_code == 500
    assert reference_response.json() == {"detail": "A database error occurred"}
    assert legal.status_code == reference_response.status_code
    assert legal.json() == reference_response.json()
    assert "SELECT" not in legal.text and "leak@example.com" not in legal.text
