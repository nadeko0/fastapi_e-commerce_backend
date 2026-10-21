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


def test_gdpr_consent_cannot_be_withdrawn_here_but_can_be_granted_again(
    client, db_session, headers
):
    refused = client.post(
        f"{USERS}/consent", params={"consent_type": "gdpr", "granted": False}, headers=headers
    )
    granted = client.post(
        f"{USERS}/consent", params={"consent_type": "gdpr", "granted": True}, headers=headers
    )

    assert refused.status_code == 400
    assert granted.status_code == 200
    user = _stored(db_session)
    assert user.gdpr_consent is True
    assert [e["type"] for e in user.consent_history].count("gdpr") == 2  # registration + grant


def test_an_unknown_consent_type_is_422(client, headers):
    response = client.post(
        f"{USERS}/consent", params={"consent_type": "telepathy", "granted": True}, headers=headers
    )
    assert response.status_code == 422


# --- export ---------------------------------------------------------------


def test_the_export_holds_the_account_its_consents_and_its_addresses(client, headers):
    client.post(f"{USERS}/addresses", headers=headers, json={
        "street": "1 Export Street", "city": "Springfield", "state": "Illinois",
        "postal_code": "62701", "country": "US",
    })
    client.post(
        f"{USERS}/consent", params={"consent_type": "marketing", "granted": True}, headers=headers
    )

    response = client.get(f"{USERS}/data/export", headers=headers)

    data = response.json()["data"]
    assert data["personal_data"]["email"] == EMAIL
    assert "hashed_password" not in data["personal_data"]
    assert [a["street"] for a in data["addresses"]] == ["1 Export Street"]
    assert [c["type"] for c in data["consents"]] == [
        "gdpr", "privacy_policy", "marketing", "marketing",
    ]
    assert data["orders"] == []
    metadata = data["export_metadata"]
    assert metadata["status"] == "completed"
    assert metadata["request_id"].startswith(f"export_{data['personal_data']['id']}_")


def test_the_export_is_also_announced_by_email(client, monkeypatch, headers):
    sent = []
    monkeypatch.setattr(
        "app.api.v1.users.send_gdpr_export_email", lambda to_email, meta: sent.append(to_email)
    )

    client.get(f"{USERS}/data/export", headers=headers)

    assert sent == [EMAIL]


def test_one_users_export_never_contains_another_users_data(client, headers):
    client.post(f"{USERS}/register", json={
        "email": "other.person@example.com", "password": PASSWORD, "full_name": "Other",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })
    other = client.post(
        f"{USERS}/login", data={"username": "other.person@example.com", "password": PASSWORD}
    ).json()["data"]["access_token"]
    client.post(f"{USERS}/addresses", headers={"Authorization": f"Bearer {other}"}, json={
        "street": "9 Other Road", "city": "Springfield", "state": "Illinois",
        "postal_code": "62701", "country": "US",
    })

    data = client.get(f"{USERS}/data/export", headers=headers).json()["data"]

    assert data["addresses"] == []
    assert "other.person@example.com" not in str(data)


# --- erasure --------------------------------------------------------------


def test_erasure_without_confirmation_is_422_and_changes_nothing(client, db_session, headers):
    response = client.post(
        f"{USERS}/data/delete", json={"confirmation": False, "password": PASSWORD}, headers=headers
    )

    assert response.status_code == 422
    assert _stored(db_session).is_active is True


def test_erasure_schedules_the_account_and_ends_its_sessions(client, db_session, headers):
    response = client.post(
        f"{USERS}/data/delete", json={"confirmation": True, "password": PASSWORD}, headers=headers
    )

    assert response.status_code == 200
    user = _stored(db_session)
    assert (user.is_active, user.data_deletion_requested) == (False, True)
    assert user.data_deletion_date is not None
    assert client.get(f"{USERS}/me", headers=headers).status_code == 400
    # And a second request is refused: the account is no longer active.
    again = client.post(
        f"{USERS}/data/delete", json={"confirmation": True, "password": PASSWORD}, headers=headers
    )
    assert again.status_code == 400
