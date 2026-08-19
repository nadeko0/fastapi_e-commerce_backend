from app.core.security import blacklist_token

USERS_PREFIX = "/api/v1/users"
LEGAL_PREFIX = "/api/v1/legal"


def _valid_registration_payload():
    return {
        "email": "legal.user@example.com",
        "password": "Str0ngPass1",
        "full_name": "Legal User",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }


def _register_and_login(client, payload):
    client.post(f"{USERS_PREFIX}/register", json=payload)
    login_response = client.post(
        f"{USERS_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return login_response.json()["data"]["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _assert_envelope(body):
    """Every router in this API wraps responses in the APIResponse envelope
    (success/data/error/timestamp/request_id) - see app/schemas/common.py.
    legal.py used to return raw dicts/models instead; these assertions guard
    against that regressing."""
    assert set(body.keys()) >= {"success", "data", "error", "timestamp", "request_id"}
    assert body["success"] is True
    assert body["error"] is None


def test_privacy_policy_is_public(client):
    response = client.get(f"{LEGAL_PREFIX}/privacy-policy")

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert "Privacy Policy" in body["data"]["content"]


def test_terms_of_service_is_public(client):
    response = client.get(f"{LEGAL_PREFIX}/terms-of-service")

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert "Terms of Service" in body["data"]["content"]


def test_cookie_policy_is_public(client):
    response = client.get(f"{LEGAL_PREFIX}/cookie-policy")

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert "Cookie Policy" in body["data"]["content"]


def test_update_consent_requires_authentication(client):
    response = client.post(
        f"{LEGAL_PREFIX}/consent",
        json={"marketing_consent": True, "privacy_policy_accepted": True},
    )

    assert response.status_code == 401


def test_update_consent_updates_marketing_preference(client):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/consent",
        json={"marketing_consent": True, "privacy_policy_accepted": True},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert body["data"]["status"] == "success"
    assert body["data"]["updated_consents"]["marketing"] is True


def test_update_consent_succeeds_when_request_has_no_client(client, monkeypatch):
    # Regression test: update_user_consent used to read request.client.host
    # unguarded. request.client can be None (some ASGI transports don't set
    # it - Starlette's own docs note this), which would raise
    # AttributeError and turn a routine consent update into a 500, unlike
    # every other endpoint recording an IP address (see register_user/
    # update_consent in app/api/v1/users.py), which already guards against
    # this with `request.client.host if request.client else None`.
    from starlette.requests import Request

    monkeypatch.setattr(Request, "client", property(lambda self: None))

    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/consent",
        json={"marketing_consent": True, "privacy_policy_accepted": True},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert body["data"]["updated_consents"]["marketing"] is True


def test_data_request_export_returns_processing_status(client, monkeypatch):
    monkeypatch.setattr(
        "app.services.email.send_gdpr_request_received", lambda *a, **k: True
    )
    monkeypatch.setattr("app.services.email.send_gdpr_export_email", lambda *a, **k: True)
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/data-request",
        json={"request_type": "export"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert body["data"]["status"] == "processing"
    assert body["data"]["request_id"].startswith("export_")


def test_data_request_deletion_returns_processing_status(client, monkeypatch):
    monkeypatch.setattr(
        "app.services.email.send_gdpr_request_received", lambda *a, **k: True
    )
    monkeypatch.setattr(
        "app.services.email.send_gdpr_deletion_confirmation", lambda *a, **k: True
    )
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/data-request",
        json={"request_type": "deletion"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert body["data"]["request_id"].startswith("deletion_")


def test_data_request_rejects_invalid_request_type(client):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/data-request",
        json={"request_type": "not-a-real-type"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 400


def test_consent_status_returns_current_state(client):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.get(f"{LEGAL_PREFIX}/consent-status", headers=_auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert body["data"]["marketing_consent"] is False
    assert body["data"]["privacy_policy_accepted"] is True


def test_data_retention_reports_within_period_for_new_account(client):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.get(f"{LEGAL_PREFIX}/data-retention", headers=_auth_headers(token))

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert body["data"]["within_retention_period"] is True


def test_consent_status_rejects_blacklisted_token(client):
    # Regression test: app/api/v1/legal.py's endpoints previously accepted
    # a blacklisted (logged-out) token because they depended on a second,
    # non-auth-checking get_current_user implementation.
    token = _register_and_login(client, _valid_registration_payload())
    blacklist_token(token, expires_in=3600)

    response = client.get(f"{LEGAL_PREFIX}/consent-status", headers=_auth_headers(token))

    assert response.status_code == 401


def test_data_retention_rejects_blacklisted_token(client):
    token = _register_and_login(client, _valid_registration_payload())
    blacklist_token(token, expires_in=3600)

    response = client.get(f"{LEGAL_PREFIX}/data-retention", headers=_auth_headers(token))

    assert response.status_code == 401


def test_update_consent_rejects_blacklisted_token(client):
    token = _register_and_login(client, _valid_registration_payload())
    blacklist_token(token, expires_in=3600)

    response = client.post(
        f"{LEGAL_PREFIX}/consent",
        json={"marketing_consent": True, "privacy_policy_accepted": True},
        headers=_auth_headers(token),
    )

    assert response.status_code == 401


def test_data_request_rejects_blacklisted_token(client):
    token = _register_and_login(client, _valid_registration_payload())
    blacklist_token(token, expires_in=3600)

    response = client.post(
        f"{LEGAL_PREFIX}/data-request",
        json={"request_type": "export"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 401
