"""GDPR flows end to end through /users: export contents, consent changes
and erasure."""

import pytest

from app.models.user import User

USERS = "/api/v1/users"
EMAIL = "gdpr.flow@example.com"
PASSWORD = "Str0ngPass1"


@pytest.fixture
def headers(client):
    client.post(f"{USERS}/register", json={
        "email": EMAIL, "password": PASSWORD, "full_name": "Gdpr Flow",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })
    response = client.post(f"{USERS}/login", data={"username": EMAIL, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['data']['access_token']}"}


def _stored(db_session):
    db_session.expire_all()
    return db_session.query(User).filter(User.email == EMAIL).one()


@pytest.mark.parametrize(
    "consent_type,column,date_column",
    [
        ("marketing", "marketing_consent", "marketing_consent_date"),
        ("privacy_policy", "privacy_policy_accepted", "privacy_policy_accepted_date"),
    ],
)
def test_a_consent_change_is_stored_in_its_column_and_in_the_history(
    client, db_session, headers, consent_type, column, date_column
):
    # privacy_policy used to be written to a "privacy_policy_consent"
    # attribute the model does not have: the history said "withdrawn", the
    # stored flag stayed "accepted", and the answer was a 200.
    granted = consent_type == "marketing"  # marketing on, privacy policy off
    response = client.post(
        f"{USERS}/consent", params={"consent_type": consent_type, "granted": granted},
        headers=headers,
    )

    assert response.status_code == 200
    user = _stored(db_session)
    assert getattr(user, column) is granted
    assert getattr(user, date_column) is not None
    assert user.consent_history[-1]["type"] == consent_type
    assert user.consent_history[-1]["granted"] is granted


def test_cookie_consent_is_refused_rather_than_recorded_without_effect(
    client, db_session, headers
):
    # The account has no cookie consent column: the consent belongs to the
    # cookie banner. Recording it in the history while storing nothing
    # made the audit trail claim a consent the account does not hold.
    history_before = len(_stored(db_session).consent_history)

    response = client.post(
        f"{USERS}/consent", params={"consent_type": "cookies", "granted": True}, headers=headers
    )

    assert response.status_code == 400
    assert len(_stored(db_session).consent_history) == history_before
