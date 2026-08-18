API_PREFIX = "/api/v1/users"


def _register_and_login(client, payload):
    client.post(f"{API_PREFIX}/register", json=payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return login_response.json()["data"]["access_token"]


def test_data_export_returns_personal_data_and_consents(client, valid_registration_payload):
    # Regression test: GET /users/data/export used to construct the
    # GDPRExport schema (export *metadata*: request_id/request_date/
    # expires_at) with personal_data/consents/addresses/orders kwargs that
    # don't exist on it, raising a pydantic ValidationError on every call.
    # The actual data container is GDPRExportData.
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.get(
        f"{API_PREFIX}/data/export",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["personal_data"]["email"] == valid_registration_payload["email"]
    assert isinstance(data["consents"], list)
    assert isinstance(data["addresses"], list)
    assert isinstance(data["orders"], list)
    assert data["export_metadata"]["request_id"]
    assert data["export_metadata"]["status"] == "completed"


def test_update_consent_grants_marketing_consent(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/consent",
        params={"consent_type": "marketing", "granted": True},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["email"] == valid_registration_payload["email"]


def test_update_consent_rejects_gdpr_revocation(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/consent",
        params={"consent_type": "gdpr", "granted": False},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 400


def test_data_delete_rejects_wrong_password(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/data/delete",
        json={"confirmation": True, "password": "wrong-password"},
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 401


def test_data_delete_deactivates_account(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/data/delete",
        json={
            "confirmation": True,
            "password": valid_registration_payload["password"],
            "reason": "no longer needed",
        },
        headers={"Authorization": f"Bearer {access_token}"},
    )

    assert response.status_code == 200
    assert "deletion_date" in response.json()["data"]

    # Deactivated account can no longer authenticate.
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={
            "username": valid_registration_payload["email"],
            "password": valid_registration_payload["password"],
        },
    )
    me_response = client.get(
        f"{API_PREFIX}/me",
        headers={"Authorization": f"Bearer {login_response.json()['data']['access_token']}"},
    )
    assert me_response.status_code == 400
