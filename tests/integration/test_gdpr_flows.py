"""GDPR flows end to end through /legal: consent changes and erasure."""

import pytest

from app.models.user import User

USERS = "/api/v1/users"
LEGAL = "/api/v1/legal"
EMAIL = "gdpr.flow@example.com"
PASSWORD = "Str0ngPass1"


@pytest.fixture
def headers(client, monkeypatch):
    for name in ("send_gdpr_request_received", "send_gdpr_deletion_confirmation"):
        monkeypatch.setattr(f"app.services.email.{name}", lambda *a, **k: True)
    client.post(f"{USERS}/register", json={
        "email": EMAIL, "password": PASSWORD, "full_name": "Gdpr Flow",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })
    response = client.post(f"{USERS}/login", data={"username": EMAIL, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _stored(db_session):
    db_session.expire_all()
    return db_session.query(User).filter(User.email == EMAIL).one()


def test_withdrawing_the_privacy_policy_is_stored_in_its_column_and_in_the_history(
    client, db_session, headers
):
    # privacy_policy used to be written to a "privacy_policy_consent"
    # attribute the model does not have: the history said "withdrawn", the
    # stored flag stayed "accepted", and the answer was a 200.
    response = client.post(
        f"{LEGAL}/consent",
        json={"marketing_consent": False, "privacy_policy_accepted": False},
        headers=headers,
    )

    assert response.status_code == 200
    user = _stored(db_session)
    assert user.privacy_policy_accepted is False
    assert user.privacy_policy_accepted_date is not None
    assert user.consent_history[-1]["type"] == "privacy_policy"
    assert user.consent_history[-1]["granted"] is False


# --- erasure --------------------------------------------------------------


def test_erasure_schedules_the_account_and_ends_its_sessions(client, db_session, headers):
    request = {"request_type": "deletion", "password": PASSWORD}

    response = client.post(f"{LEGAL}/data-request", json=request, headers=headers)

    assert response.status_code == 200
    user = _stored(db_session)
    assert (user.is_active, user.data_deletion_requested) == (False, True)
    assert user.data_deletion_date is not None
    assert client.get(f"{USERS}/me", headers=headers).status_code == 403
    # A second request is refused: the account is no longer active.
    again = client.post(f"{LEGAL}/data-request", json=request, headers=headers)
    assert again.status_code == 403
    # Logging in again is refused like a wrong password.
    login = client.post(f"{USERS}/login", data={"username": EMAIL, "password": PASSWORD})
    assert login.status_code == 401
    assert login.json()["detail"] == "Incorrect email or password"
