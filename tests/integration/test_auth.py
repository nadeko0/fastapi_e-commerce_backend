from datetime import timedelta

from freezegun import freeze_time

from app.core.config import settings
from app.core.security import (
    blacklist_token,
    generate_email_verification_token,
    generate_password_reset_token,
)

API_PREFIX = "/api/v1/users"


def _register_and_login(client, payload):
    client.post(f"{API_PREFIX}/register", json=payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return login_response.json()["data"]["access_token"]


def test_register_returns_created_user(client, valid_registration_payload):
    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["email"] == valid_registration_payload["email"]
    assert "hashed_password" not in body["data"]


def test_register_rejects_duplicate_email(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 409


def test_register_requires_gdpr_consent(client, valid_registration_payload):
    valid_registration_payload["gdpr_consent"] = False
    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 422


def test_login_returns_tokens_for_valid_credentials(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": valid_registration_payload["password"],
        },
    )

    assert response.status_code == 200
    tokens = response.json()["data"]
    assert tokens["access_token"]
    assert tokens["refresh_token"]
    assert tokens["token_type"] == "bearer"


def test_login_rejects_wrong_password(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": "wrong-password",
        },
    )

    assert response.status_code == 401


def test_me_requires_authentication(client):
    response = client.get(f"{API_PREFIX}/me")

    assert response.status_code == 401


def test_me_returns_current_user_with_valid_token(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": valid_registration_payload["password"],
        },
    )
    access_token = login_response.json()["data"]["access_token"]

    response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["email"] == valid_registration_payload["email"]


def test_login_rejects_nonexistent_email_with_same_wording_as_wrong_password(
    client, valid_registration_payload
):
    # A different error message (or status) for "no such account" vs "wrong
    # password" would let an attacker enumerate registered emails.
    wrong_password_response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": "does-not-exist@example.com",
            "password": "whatever-Pass1",
        },
    )

    assert wrong_password_response.status_code == 401
    assert wrong_password_response.json()["detail"] == "Incorrect email or password"


def test_me_rejects_expired_access_token(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    with freeze_time("2026-01-01 12:00:00"):
        login_response = client.post(
            f"{API_PREFIX}/login",
            data={
                "username": valid_registration_payload["email"],
                "password": valid_registration_payload["password"],
            },
        )
        access_token = login_response.json()["data"]["access_token"]

    past_expiry = timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES + 1)
    with freeze_time("2026-01-01 12:00:00") as frozen:
        frozen.move_to(frozen.time_to_freeze + past_expiry)
        response = client.get(
            f"{API_PREFIX}/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    assert response.status_code == 401


def test_me_rejects_blacklisted_token(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    blacklist_token(access_token, expires_in=3600)

    response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 401


def test_me_rejects_token_with_tampered_signature(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)
    tampered = access_token[:-1] + ("A" if access_token[-1] != "A" else "B")

    response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {tampered}"},
    )

    assert response.status_code == 401


def test_me_rejects_refresh_token_used_as_access_token(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": valid_registration_payload["password"],
        },
    )
    refresh_token = login_response.json()["data"]["refresh_token"]

    response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {refresh_token}"},
    )

    assert response.status_code == 401


def test_me_rejects_password_reset_token_used_as_access_token(
    client, valid_registration_payload
):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    reset_token = generate_password_reset_token(valid_registration_payload["email"])

    response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {reset_token}"},
    )

    assert response.status_code == 401


def test_verify_email_activates_account(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    token = generate_email_verification_token(valid_registration_payload["email"])

    response = client.get(f"{API_PREFIX}/verify-email/{token}")

    assert response.status_code == 200
    assert response.json()["data"]["message"] == "Email verified successfully"


def test_verify_email_rejects_invalid_token(client):
    response = client.get(f"{API_PREFIX}/verify-email/not-a-real-token")

    assert response.status_code == 400


def test_verify_email_twice_reports_already_verified(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    token = generate_email_verification_token(valid_registration_payload["email"])
    client.get(f"{API_PREFIX}/verify-email/{token}")

    response = client.get(f"{API_PREFIX}/verify-email/{token}")

    assert response.status_code == 200
    assert response.json()["data"]["message"] == "Email already verified"


def test_password_reset_flow_allows_login_with_new_password(client, valid_registration_payload):
    client.post(f"{API_PREFIX}/register", json=valid_registration_payload)
    reset_token = generate_password_reset_token(valid_registration_payload["email"])

    validate_response = client.get(f"{API_PREFIX}/password/reset/{reset_token}")
    assert validate_response.status_code == 200

    reset_response = client.post(
        f"{API_PREFIX}/password/reset/{reset_token}",
        json={"current_password": "unused", "new_password": "NewStr0ngPass"},
    )
    assert reset_response.status_code == 200

    old_password_login = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": valid_registration_payload["password"],
        },
    )
    assert old_password_login.status_code == 401

    new_password_login = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": "NewStr0ngPass",
        },
    )
    assert new_password_login.status_code == 200


def test_password_reset_rejects_invalid_token(client):
    response = client.post(
        f"{API_PREFIX}/password/reset/not-a-real-token",
        json={"current_password": "unused", "new_password": "NewStr0ngPass"},
    )

    assert response.status_code == 400
