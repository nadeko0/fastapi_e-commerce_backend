API_PREFIX = "/api/v1/users"


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
