API_PREFIX = "/api/v1/users"


def _register_and_login(client, payload):
    client.post(f"{API_PREFIX}/register", json=payload)
    login_response = client.post(
        f"{API_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return login_response.json()["data"]["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _valid_address():
    return {
        "street": "123 Main Street",
        "city": "Springfield",
        "state": "Illinois",
        "postal_code": "62701",
        "country": "US",
    }


# --- Registration validation edge cases -----------------------------------


def test_register_rejects_weak_password_without_digit(client, valid_registration_payload):
    valid_registration_payload["password"] = "OnlyLetters"

    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 422


def test_register_rejects_weak_password_without_letter(client, valid_registration_payload):
    valid_registration_payload["password"] = "12345678"

    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 422


def test_register_rejects_password_over_72_bytes(client, valid_registration_payload):
    # bcrypt hard-limits inputs to 72 bytes; see app/schemas/user.py's
    # validate_password_strength.
    valid_registration_payload["password"] = "Aa1" + "b" * 70

    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 422


def test_register_rejects_missing_privacy_policy_acceptance(client, valid_registration_payload):
    valid_registration_payload["privacy_policy_accepted"] = False

    response = client.post(f"{API_PREFIX}/register", json=valid_registration_payload)

    assert response.status_code == 422


# --- Profile update ---------------------------------------------------------


def test_update_profile_changes_full_name(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.put(
        f"{API_PREFIX}/me",
        json={"full_name": "Jane Updated"},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["full_name"] == "Jane Updated"


def test_update_profile_rejects_empty_body(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.put(
        f"{API_PREFIX}/me",
        json={},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 400


# --- Address CRUD ------------------------------------------------------------


def test_create_address_becomes_default_when_first(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.post(
        f"{API_PREFIX}/addresses",
        json=_valid_address(),
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["is_default"] is True


def test_list_addresses_returns_created_address(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)
    client.post(
        f"{API_PREFIX}/addresses", json=_valid_address(), headers=_auth_headers(access_token)
    )

    response = client.get(f"{API_PREFIX}/addresses", headers=_auth_headers(access_token))

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["total"] == 1
    assert len(body["items"]) == 1


def test_update_address_changes_city(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)
    create_response = client.post(
        f"{API_PREFIX}/addresses", json=_valid_address(), headers=_auth_headers(access_token)
    )
    address_id = create_response.json()["data"]["id"]

    response = client.put(
        f"{API_PREFIX}/addresses/{address_id}",
        json={"city": "New City"},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["city"] == "New City"


def test_update_nonexistent_address_returns_404(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.put(
        f"{API_PREFIX}/addresses/999999",
        json={"city": "New City"},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 404


def test_set_default_address(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)
    first = client.post(
        f"{API_PREFIX}/addresses", json=_valid_address(), headers=_auth_headers(access_token)
    ).json()["data"]
    second_payload = _valid_address()
    second_payload["street"] = "456 Other Street"
    second = client.post(
        f"{API_PREFIX}/addresses", json=second_payload, headers=_auth_headers(access_token)
    ).json()["data"]
    assert first["is_default"] is True
    assert second["is_default"] is False

    response = client.post(
        f"{API_PREFIX}/addresses/default",
        json={"address_id": second["id"]},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 200

    listing = client.get(f"{API_PREFIX}/addresses", headers=_auth_headers(access_token))
    items = {item["id"]: item["is_default"] for item in listing.json()["data"]["items"]}
    assert items[second["id"]] is True
    assert items[first["id"]] is False


def test_delete_address_promotes_another_to_default(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)
    first = client.post(
        f"{API_PREFIX}/addresses", json=_valid_address(), headers=_auth_headers(access_token)
    ).json()["data"]
    second_payload = _valid_address()
    second_payload["street"] = "456 Other Street"
    second = client.post(
        f"{API_PREFIX}/addresses", json=second_payload, headers=_auth_headers(access_token)
    ).json()["data"]

    response = client.delete(
        f"{API_PREFIX}/addresses/{first['id']}", headers=_auth_headers(access_token)
    )

    assert response.status_code == 200

    listing = client.get(f"{API_PREFIX}/addresses", headers=_auth_headers(access_token))
    remaining_ids = {item["id"] for item in listing.json()["data"]["items"]}
    assert first["id"] not in remaining_ids
    assert listing.json()["data"]["total"] == 1
    assert second["id"] in remaining_ids


def test_delete_nonexistent_address_returns_404(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.delete(
        f"{API_PREFIX}/addresses/999999", headers=_auth_headers(access_token)
    )

    assert response.status_code == 404


# --- Cross-user authorization -------------------------------------------------


def _second_user_payload():
    return {
        "email": "second.user@example.com",
        "password": "Str0ngPass2",
        "full_name": "Second User",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }


def test_user_cannot_read_another_users_address_by_guessing_id(
    client, valid_registration_payload
):
    owner_token = _register_and_login(client, valid_registration_payload)
    owned_address = client.post(
        f"{API_PREFIX}/addresses", json=_valid_address(), headers=_auth_headers(owner_token)
    ).json()["data"]

    attacker_token = _register_and_login(client, _second_user_payload())

    # There is no GET /addresses/{id}; the update/delete routes are where
    # cross-user access must be blocked instead of leaking existence.
    response = client.put(
        f"{API_PREFIX}/addresses/{owned_address['id']}",
        json={"city": "Hijacked City"},
        headers=_auth_headers(attacker_token),
    )

    assert response.status_code == 404


def test_user_cannot_delete_another_users_address_by_guessing_id(
    client, valid_registration_payload
):
    owner_token = _register_and_login(client, valid_registration_payload)
    owned_address = client.post(
        f"{API_PREFIX}/addresses", json=_valid_address(), headers=_auth_headers(owner_token)
    ).json()["data"]

    attacker_token = _register_and_login(client, _second_user_payload())

    response = client.delete(
        f"{API_PREFIX}/addresses/{owned_address['id']}",
        headers=_auth_headers(attacker_token),
    )

    assert response.status_code == 404

    # Confirm it wasn't actually deleted for the real owner.
    listing = client.get(f"{API_PREFIX}/addresses", headers=_auth_headers(owner_token))
    assert listing.json()["data"]["total"] == 1
