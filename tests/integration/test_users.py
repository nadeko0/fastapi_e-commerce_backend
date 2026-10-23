import pytest

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


def test_register_rejects_blocklisted_password(client, valid_registration_payload):
    valid_registration_payload["password"] = "qwertyuiop"

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


def test_update_profile_returns_404_when_user_deleted_after_token_issued(
    client, valid_registration_payload, monkeypatch
):
    # Regression test: PUT /users/me raised HTTPException(404, "User not
    # found") inside a try block whose `except Exception` also catches
    # HTTPException (it IS an Exception), rewriting it to a 500 with a
    # generic body. This is reachable when the user row disappears (e.g. a
    # concurrent GDPR deletion) *between* get_current_active_user resolving
    # current_user (its own AsyncSession, a select() through
    # app.core.database.get_async_db) and this route's own re-query of the
    # same user (the same AsyncSession, through the dependency cache).
    # The auth dependency no longer goes through Query.first(), so the
    # route's re-query is the only such call in this request; simulate the
    # race deterministically by making it return None as if the row were
    # gone by then.
    access_token = _register_and_login(client, valid_registration_payload)

    from sqlalchemy.ext.asyncio import AsyncSession

    call_count = {"n": 0}

    async def patched_get(self, *args, **kwargs):
        call_count["n"] += 1
        return None

    # The route re-loads the user with db.get(); the auth dependency reads
    # it with execute(), so only the route's lookup is affected.
    monkeypatch.setattr(AsyncSession, "get", patched_get)

    response = client.put(
        f"{API_PREFIX}/me",
        json={"full_name": "Ghost User"},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "User not found"
    # Exactly the route's one lookup - authentication itself succeeded.
    assert call_count["n"] == 1


def test_update_profile_returns_500_on_genuine_db_error(
    client, valid_registration_payload, monkeypatch
):
    # Companion regression test: confirm the fix to the 404 case above didn't
    # simply remove error handling altogether - a real unexpected failure
    # during the update (here, db.commit() raising) must still produce a 500.
    access_token = _register_and_login(client, valid_registration_payload)

    from sqlalchemy.ext.asyncio import AsyncSession

    original_commit = AsyncSession.commit
    commits = {"n": 0}

    async def _boom(self, *args, **kwargs):
        # The auth dependency shares the route's session and commits first
        # (it ends its read transaction); this test is about the update
        # failing, not authentication, so only the commits after that fail.
        commits["n"] += 1
        if commits["n"] == 1:
            return await original_commit(self, *args, **kwargs)
        raise RuntimeError("simulated database failure")

    monkeypatch.setattr(AsyncSession, "commit", _boom)

    response = client.put(
        f"{API_PREFIX}/me",
        json={"full_name": "Should Fail"},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 500
    assert response.json()["detail"] == "Failed to update profile"


def test_update_profile_rejects_empty_body(client, valid_registration_payload):
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.put(
        f"{API_PREFIX}/me",
        json={},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 400


def test_update_profile_rejects_null_marketing_consent(client, valid_registration_payload):
    # users.marketing_consent is NOT NULL; null used to end as a 500
    # "Failed to update profile" from the IntegrityError.
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.put(
        f"{API_PREFIX}/me",
        json={"marketing_consent": None},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 422


@pytest.mark.parametrize("field", ["full_name", "phone"])
def test_update_profile_null_clears_nullable_field(client, valid_registration_payload, field):
    valid_registration_payload["phone"] = "+123456789"
    access_token = _register_and_login(client, valid_registration_payload)

    response = client.put(
        f"{API_PREFIX}/me",
        json={field: None},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 200
    assert response.json()["data"][field] is None


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


# These address columns are NOT NULL; null used to end as a 500 from the
# IntegrityError on commit.
@pytest.mark.parametrize(
    "field",
    ["street", "city", "state", "postal_code", "country", "address_type", "is_default"],
)
def test_update_address_rejects_explicit_null(client, valid_registration_payload, field):
    access_token = _register_and_login(client, valid_registration_payload)
    created = client.post(
        f"{API_PREFIX}/addresses", json=_valid_address(), headers=_auth_headers(access_token)
    ).json()["data"]

    response = client.put(
        f"{API_PREFIX}/addresses/{created['id']}",
        json={field: None},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 422
    listing = client.get(f"{API_PREFIX}/addresses", headers=_auth_headers(access_token))
    assert listing.json()["data"]["items"][0][field] == created[field]


@pytest.mark.parametrize("field", ["delivery_instructions", "delivery_phone"])
def test_update_address_null_clears_nullable_field(client, valid_registration_payload, field):
    access_token = _register_and_login(client, valid_registration_payload)
    address = {
        **_valid_address(),
        "delivery_instructions": "Leave at the door",
        "delivery_phone": "+123456789",
    }
    created = client.post(
        f"{API_PREFIX}/addresses", json=address, headers=_auth_headers(access_token)
    ).json()["data"]

    response = client.put(
        f"{API_PREFIX}/addresses/{created['id']}",
        json={field: None},
        headers=_auth_headers(access_token),
    )

    assert response.status_code == 200
    assert response.json()["data"][field] is None


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
