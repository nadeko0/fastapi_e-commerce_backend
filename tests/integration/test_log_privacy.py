"""No personal data in the logs.

Logs go to stdout and, optionally, a rotating file (app/core/logging_config.py), are
kept with no retention period of their own and are not scrubbed when an
account is erased. So they carry user ids, never emails, names, phone
numbers or the values of profile fields.
"""

import logging

import pytest

from app.core.security import generate_email_verification_token

USERS = "/api/v1/users"
EMAIL = "pii.person@example.com"
PASSWORD = "Str0ngPass1"
NAME = "Personal Name"
NEW_NAME = "Changed Name"
PHONE = "+15550001111"


@pytest.fixture
def every_log_line(caplog):
    caplog.set_level(logging.DEBUG)
    # The SQLite driver the tests run on (aiosqlite) logs every statement
    # with its bound parameters at DEBUG; the production driver does not.
    # That is the test database's chatter, not the application's logging.
    caplog.set_level(logging.INFO, logger="aiosqlite")
    return caplog


def _assert_no_personal_data(caplog):
    text = caplog.text + "".join(str(record.args) for record in caplog.records)
    for value in (EMAIL, NAME, NEW_NAME, PHONE, PHONE.lstrip("+")):
        assert value not in text, f"{value!r} was logged"


def _login(client):
    response = client.post(f"{USERS}/login", data={"username": EMAIL, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_the_account_lifecycle_logs_no_personal_data(client, every_log_line, monkeypatch):
    monkeypatch.setattr("app.api.v1.users.send_password_reset_email", lambda *a, **k: True)
    client.post(f"{USERS}/register", json={
        "email": EMAIL, "password": PASSWORD, "full_name": NAME, "phone": PHONE,
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })
    client.post(f"{USERS}/register", json={
        "email": EMAIL, "password": PASSWORD, "full_name": NAME,
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })
    headers = _login(client)

    assert client.put(
        f"{USERS}/me", json={"full_name": NEW_NAME, "phone": "+15550002222"}, headers=headers
    ).status_code == 200
    client.post(f"{USERS}/verify-email/resend", headers=headers)
    client.post(f"{USERS}/verify-email", json={"token": generate_email_verification_token(EMAIL)})
    address = client.post(f"{USERS}/addresses", headers=headers, json={
        "street": "1 Main Street", "city": "Springfield", "state": "Illinois",
        "postal_code": "62701", "country": "US",
    }).json()
    client.put(f"{USERS}/addresses/{address['id']}", json={"city": "Shelbyville"}, headers=headers)
    client.post(f"{USERS}/addresses/default", json={"address_id": address["id"]}, headers=headers)
    client.delete(f"{USERS}/addresses/{address['id']}", headers=headers)
    client.post(f"{USERS}/password/reset", json={"email": EMAIL})
    client.post(
        "/api/v1/legal/data-request", json={"request_type": "deletion", "password": PASSWORD}, headers=headers
    )

    _assert_no_personal_data(every_log_line)


def test_a_failed_profile_update_logs_no_personal_data(
    client, every_log_line, db_session, monkeypatch
):
    client.post(f"{USERS}/register", json={
        "email": EMAIL, "password": PASSWORD, "full_name": "Someone",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })
    headers = _login(client)

    from sqlalchemy.ext.asyncio import AsyncSession

    original_commit = AsyncSession.commit
    commits = {"n": 0}

    async def broken_commit(self, *args, **kwargs):
        # The first commit is the auth dependency's (shared session): see
        # test_update_profile_returns_500_on_genuine_db_error.
        commits["n"] += 1
        if commits["n"] == 1:
            return await original_commit(self, *args, **kwargs)
        raise RuntimeError(f"database said no to {NEW_NAME}")

    monkeypatch.setattr(AsyncSession, "commit", broken_commit)
    response = client.put(f"{USERS}/me", json={"full_name": NEW_NAME}, headers=headers)

    assert response.status_code == 500
    # The exception text can carry the values; only its type is logged.
    _assert_no_personal_data(every_log_line)
