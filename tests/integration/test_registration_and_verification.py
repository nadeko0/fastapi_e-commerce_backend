"""Registration and email verification, the edge cases: what is stored,
what is refused, and what a verification link can and cannot do."""

from datetime import timedelta

import pytest
from freezegun import freeze_time

from app.core.config import settings
from app.core.security import (
    create_access_token,
    generate_email_verification_token,
    generate_password_reset_token,
)
from app.models.user import User

USERS = "/api/v1/users"
PASSWORD = "Str0ngPass1"


def _registration(**overrides):
    return {
        "email": "new.person@example.com",
        "password": PASSWORD,
        "full_name": "New Person",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        **overrides,
    }


def _stored(db_session, email="new.person@example.com"):
    db_session.expire_all()
    return db_session.query(User).filter(User.email == email).one()


def _login(client, email="new.person@example.com"):
    response = client.post(f"{USERS}/login", data={"username": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


# --- registration ---------------------------------------------------------


def test_a_new_account_starts_as_an_unverified_active_client(client, db_session):
    client.post(f"{USERS}/register", json=_registration())

    user = _stored(db_session)
    assert user.role == "client"
    assert user.is_active is True
    assert user.is_email_verified is False
    assert user.hashed_password != PASSWORD and user.hashed_password.startswith("$2b$")


def test_registration_records_each_consent_with_when_and_from_where(client, db_session):
    client.post(
        f"{USERS}/register",
        json=_registration(marketing_consent=True),
        headers={"User-Agent": "signup-agent"},
    )

    user = _stored(db_session)
    assert user.gdpr_consent_date is not None
    assert user.privacy_policy_accepted_date is not None
    assert user.marketing_consent is True and user.marketing_consent_date is not None
    assert [(entry["type"], entry["granted"]) for entry in user.consent_history] == [
        ("gdpr", True), ("privacy_policy", True), ("marketing", True),
    ]
    assert {entry["user_agent"] for entry in user.consent_history} == {"signup-agent"}


def test_no_marketing_consent_means_no_marketing_consent_date(client, db_session):
    client.post(f"{USERS}/register", json=_registration())

    user = _stored(db_session)
    assert user.marketing_consent is False
    assert user.marketing_consent_date is None


def test_a_client_cannot_register_itself_as_an_admin(client, db_session):
    # Extra fields are refused, not obeyed: nothing is created.
    response = client.post(
        f"{USERS}/register", json=_registration(role="admin", is_email_verified=True))

    assert response.status_code == 422
    assert {tuple(e["loc"]) for e in response.json()["errors"]} == {
        ("body", "role"), ("body", "is_email_verified")}
    assert db_session.query(User).count() == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"full_name": "A"},
        {"full_name": "x" * 101},
        {"phone": "12345"},
        {"phone": "+12"},
        {"phone": "+0000000"},
        {"phone": "+1234567890123456"},
        {"email": "not-an-email"},
        {"gdpr_consent": False},
        {"privacy_policy_accepted": False},
        {"password": "short1"},
    ],
    ids=[
        "name-too-short", "name-too-long", "phone-without-plus", "phone-too-short", "phone-leading-zero", "phone-over-15-digits",
        "bad-email", "no-gdpr-consent", "no-privacy-policy", "short-password",
    ],
)
def test_invalid_registrations_are_422_and_create_nothing(client, db_session, overrides):
    response = client.post(f"{USERS}/register", json=_registration(**overrides))

    assert response.status_code == 422
    assert db_session.query(User).count() == 0


def test_a_missing_required_field_is_422(client):
    payload = _registration()
    del payload["password"]
    assert client.post(f"{USERS}/register", json=payload).status_code == 422


# --- email verification ---------------------------------------------------


def test_verification_marks_the_account_verified_with_a_date(client, db_session):
    client.post(f"{USERS}/register", json=_registration())

    response = client.post(f"{USERS}/verify-email", json={"token": generate_email_verification_token('new.person@example.com')})

    assert response.status_code == 200
    user = _stored(db_session)
    assert user.is_email_verified is True
    assert user.email_verification_date is not None


def test_an_expired_verification_link_is_refused(client, db_session):
    client.post(f"{USERS}/register", json=_registration())
    with freeze_time("2026-01-01 12:00:00") as clock:
        token = generate_email_verification_token("new.person@example.com")
        clock.tick(timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS, seconds=1))
        response = client.post(f"{USERS}/verify-email", json={"token": token})

    assert response.status_code == 400
    assert _stored(db_session).is_email_verified is False


@pytest.mark.parametrize("kind", ["access", "reset"])
def test_another_kind_of_token_does_not_verify_an_email(client, db_session, kind):
    client.post(f"{USERS}/register", json=_registration())
    user = _stored(db_session)
    token = (
        create_access_token(user.id)
        if kind == "access"
        else generate_password_reset_token(user.email, user.hashed_password)
    )

    assert client.post(f"{USERS}/verify-email", json={"token": token}).status_code == 400
    assert _stored(db_session).is_email_verified is False


def test_a_verification_link_for_an_account_that_is_gone_is_404(client):
    token = generate_email_verification_token("nobody@example.com")

    response = client.post(f"{USERS}/verify-email", json={"token": token})

    assert response.status_code == 404


def test_resend_queues_a_fresh_link_for_an_unverified_account(client, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.api.v1.users.send_email_verification",
        lambda to_email, token: sent.append((to_email, token)),
    )
    client.post(f"{USERS}/register", json=_registration())

    response = client.post(f"{USERS}/verify-email/resend", headers=_login(client))

    assert response.status_code == 200
    assert [to for to, _ in sent] == ["new.person@example.com"]
    # The link in it works.
    assert client.post(f"{USERS}/verify-email", json={"token": sent[0][1]}).status_code == 200


def test_resend_for_a_verified_account_is_409(client):
    client.post(f"{USERS}/register", json=_registration())
    client.post(f"{USERS}/verify-email", json={"token": generate_email_verification_token('new.person@example.com')})

    response = client.post(f"{USERS}/verify-email/resend", headers=_login(client))

    assert response.status_code == 409
    assert response.json()["detail"] == "Email already verified"


def test_resend_needs_a_login(client):
    assert client.post(f"{USERS}/verify-email/resend").status_code == 401


def test_verification_is_idempotent_and_the_body_is_validated(client):
    client.post(f"{USERS}/register", json=_registration())
    token = generate_email_verification_token("new.person@example.com")

    first = client.post(f"{USERS}/verify-email", json={"token": token})
    again = client.post(f"{USERS}/verify-email", json={"token": token})

    assert first.json()["message"] == "Email verified successfully"
    assert again.json()["message"] == "Email already verified"
    assert client.post(f"{USERS}/verify-email", json={"token": "garbage"}).status_code == 400
    assert client.post(f"{USERS}/verify-email", json={}).status_code == 422
    assert client.post(f"{USERS}/verify-email", json={"token": ""}).status_code == 422
