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


# --- Regression: the routes that change the user must persist the change ----
#
# POST /legal/consent and POST /legal/data-request (deletion) mutate the
# user. They used to mutate current_user itself and commit their own `db`,
# which only worked because the auth dependency happened to resolve through
# the same deps.get_db and so shared that session. Auth now loads the user
# through its own AsyncSession and returns a detached object, so these
# routes load the row in their own session. The tests below check what is
# actually stored, through a separate session and through later requests -
# the response alone would look right even if nothing had been saved.


def _stored_user(db_session, email="legal.user@example.com"):
    from app.models.user import User

    db_session.expire_all()
    return db_session.query(User).filter(User.email == email).one()


def test_update_consent_is_persisted_and_visible_on_later_requests(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    before = client.get(f"{LEGAL_PREFIX}/consent-status", headers=_auth_headers(token))
    assert before.json()["data"]["marketing_consent"] is False
    history_before = len(before.json()["data"]["consent_history"])

    response = client.post(
        f"{LEGAL_PREFIX}/consent",
        json={"marketing_consent": True, "privacy_policy_accepted": True},
        headers={**_auth_headers(token), "User-Agent": "pytest-agent"},
    )
    assert response.status_code == 200

    # A later request, with its own sessions, sees the new state.
    after = client.get(f"{LEGAL_PREFIX}/consent-status", headers=_auth_headers(token))
    assert after.status_code == 200
    data = after.json()["data"]
    assert data["marketing_consent"] is True
    assert data["marketing_consent_date"] is not None
    assert len(data["consent_history"]) == history_before + 1
    assert data["consent_history"][-1]["type"] == "marketing"
    assert data["consent_history"][-1]["granted"] is True
    assert data["consent_history"][-1]["user_agent"] == "pytest-agent"

    # And so does the database, read through a session of the test's own.
    stored = _stored_user(db_session)
    assert stored.marketing_consent is True
    assert stored.marketing_consent_date is not None
    assert stored.consent_history[-1]["type"] == "marketing"


def test_withdrawing_consent_is_persisted_too(client, db_session):
    payload = {**_valid_registration_payload(), "marketing_consent": True}
    token = _register_and_login(client, payload)

    response = client.post(
        f"{LEGAL_PREFIX}/consent",
        json={"marketing_consent": False, "privacy_policy_accepted": True},
        headers=_auth_headers(token),
    )
    assert response.status_code == 200
    assert response.json()["data"]["updated_consents"]["marketing"] is False

    after = client.get(f"{LEGAL_PREFIX}/consent-status", headers=_auth_headers(token))
    assert after.json()["data"]["marketing_consent"] is False
    assert _stored_user(db_session).marketing_consent is False


def test_unchanged_consent_adds_no_history_entry(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    history_before = len(_stored_user(db_session).consent_history)

    response = client.post(
        f"{LEGAL_PREFIX}/consent",
        json={"marketing_consent": False, "privacy_policy_accepted": True},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert len(_stored_user(db_session).consent_history) == history_before


def test_data_request_deletion_actually_deactivates_the_account(client, db_session, monkeypatch):
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

    stored = _stored_user(db_session)
    assert stored.is_active is False
    assert stored.data_deletion_requested is True
    assert stored.data_deletion_date is not None

    # The account is deactivated for every later request with this token.
    later = client.get(f"{USERS_PREFIX}/me", headers=_auth_headers(token))
    assert later.status_code == 400
    assert later.json()["detail"] == "Inactive user"


def test_data_request_export_leaves_the_account_untouched(client, db_session, monkeypatch):
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
    stored = _stored_user(db_session)
    assert response.json()["data"]["request_id"].startswith(f"export_{stored.id}_")
    assert stored.is_active is True
    assert stored.data_deletion_requested is False


def test_consent_update_for_a_user_deleted_mid_request_is_a_404(client, db_session, monkeypatch):
    # Authenticated, then gone before the route loads the row itself (the
    # route's lookup is the only Query.first() of the request).
    import sqlalchemy.orm

    token = _register_and_login(client, _valid_registration_payload())
    monkeypatch.setattr(sqlalchemy.orm.Query, "first", lambda self: None)

    response = client.post(
        f"{LEGAL_PREFIX}/consent",
        json={"marketing_consent": True, "privacy_policy_accepted": True},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "User not found"


def test_data_retention_reports_the_users_own_retention_period(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.get(f"{LEGAL_PREFIX}/data-retention", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"]["retention_days"] == _stored_user(db_session).data_retention_period
