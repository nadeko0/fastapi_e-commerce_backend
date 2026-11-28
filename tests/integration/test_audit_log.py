"""Security audit events (app/core/audit.py): each security-relevant event
writes one `app.audit` line with a stable `event` name and ids, never the
email, password or token."""

import logging

import pytest
from conftest import TestingSessionLocal

from app.core.security import generate_password_reset_token
from app.models.user import User
from tests.integration.test_admin import _make_admin

USERS = "/api/v1/users"
ADMIN = "/api/v1/admin"


@pytest.fixture
def audit_log(caplog):
    caplog.set_level(logging.INFO, logger="app.audit")
    return caplog


def _events(caplog, name):
    return [r for r in caplog.records if r.name == "app.audit" and getattr(r, "event", None) == name]


def _register(client, payload):
    client.post(f"{USERS}/register", json=payload)
    with TestingSessionLocal() as db:
        return db.query(User).filter(User.email == payload["email"]).one()


def _login(client, email, password):
    return client.post(f"{USERS}/login", data={"username": email, "password": password})


def _no_secrets(caplog, *secrets):
    for record in caplog.records:
        if record.name != "app.audit":
            continue
        text = record.getMessage() + repr(record.__dict__)
        for secret in secrets:
            assert secret not in text


def test_a_wrong_password_is_a_login_failed_event(client, audit_log, valid_registration_payload):
    user = _register(client, valid_registration_payload)

    assert _login(client, user.email, "Wr0ngPassword").status_code == 401

    [event] = _events(audit_log, "login.failed")
    assert event.levelno == logging.WARNING
    assert event.user_id == user.id
    assert event.reason == "wrong_password"
    _no_secrets(audit_log, user.email, "Wr0ngPassword")


def test_an_unknown_email_is_logged_without_the_email(client, audit_log):
    assert _login(client, "nobody@example.com", "Str0ngPass1").status_code == 401

    [event] = _events(audit_log, "login.failed")
    assert event.user_id is None
    assert event.reason == "unknown_account"
    _no_secrets(audit_log, "nobody@example.com")


def test_a_replayed_refresh_token_is_a_warning(client, audit_log, valid_registration_payload):
    user = _register(client, valid_registration_payload)
    first = _login(client, user.email, valid_registration_payload["password"]).json()["refresh_token"]
    assert client.post(f"{USERS}/refresh", json={"refresh_token": first}).status_code == 200

    assert client.post(f"{USERS}/refresh", json={"refresh_token": first}).status_code == 401

    [event] = _events(audit_log, "refresh.replay")
    assert event.levelno == logging.WARNING
    assert event.user_id == user.id
    _no_secrets(audit_log, first)


def test_a_customer_on_an_admin_route_is_admin_denied(client, audit_log, valid_registration_payload):
    user = _register(client, valid_registration_payload)
    token = _login(client, user.email, valid_registration_payload["password"]).json()["access_token"]

    response = client.post(f"{ADMIN}/categories", json={"name": "Denied"},
                           headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 403
    [event] = _events(audit_log, "admin.denied")
    assert (event.user_id, event.method, event.path) == (user.id, "POST", f"{ADMIN}/categories")


def test_an_admin_change_is_logged_and_a_read_is_not(client, db_session, audit_log):
    admin = _make_admin(db_session)
    token = _login(client, admin.email, "AdminPass1").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    client.get(f"{ADMIN}/orders", headers=headers)
    assert _events(audit_log, "admin.action") == []

    assert client.post(f"{ADMIN}/categories", json={"name": "Audited"}, headers=headers).status_code == 201
    [event] = _events(audit_log, "admin.action")
    assert (event.user_id, event.method, event.path) == (admin.id, "POST", f"{ADMIN}/categories")


def test_a_password_reset_is_logged(client, audit_log, valid_registration_payload):
    user = _register(client, valid_registration_payload)
    token = generate_password_reset_token(user.email, user.hashed_password)

    response = client.post(f"{USERS}/password/reset/confirm",
                           json={"token": token, "new_password": "NewStr0ngPass"})

    assert response.status_code == 200
    [event] = _events(audit_log, "password.reset")
    assert event.user_id == user.id
    _no_secrets(audit_log, "NewStr0ngPass", token)


def test_logout_is_logged_with_its_scope(client, audit_log, valid_registration_payload):
    user = _register(client, valid_registration_payload)
    token = _login(client, user.email, valid_registration_payload["password"]).json()["access_token"]

    assert client.post(f"{USERS}/logout", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    [event] = _events(audit_log, "logout")
    assert (event.user_id, event.all_devices) == (user.id, True)


def test_a_data_export_request_is_logged(client, audit_log, export_builds, valid_registration_payload):
    user = _register(client, valid_registration_payload)
    token = _login(client, user.email, valid_registration_payload["password"]).json()["access_token"]

    response = client.post("/api/v1/legal/data-request", json={"request_type": "export"},
                           headers={"Authorization": f"Bearer {token}"})

    assert response.status_code in (200, 202)
    [event] = _events(audit_log, "gdpr.export_requested")
    assert event.user_id == user.id
    assert event.request_id == response.json()["request_id"]
