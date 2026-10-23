import asyncio

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
        json={"request_type": "deletion", "password": _valid_registration_payload()["password"]},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()
    _assert_envelope(body)
    assert body["data"]["request_id"].startswith("deletion_")


def _quiet_gdpr_mail(monkeypatch):
    for name in (
        "send_gdpr_request_received",
        "send_gdpr_deletion_confirmation",
        "send_gdpr_export_email",
    ):
        monkeypatch.setattr(f"app.services.email.{name}", lambda *a, **k: True)


def test_data_request_deletion_without_the_password_changes_nothing(
    client, db_session, monkeypatch
):
    # Erasure is irreversible once the grace period is over. A bearer token
    # alone (15 minutes of a stolen session) must not be enough for it:
    # /users/data/delete asks for the password, and so must this route.
    _quiet_gdpr_mail(monkeypatch)
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/data-request",
        json={"request_type": "deletion"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 422
    stored = _stored_user(db_session)
    assert stored.is_active is True
    assert stored.data_deletion_requested is False


def test_data_request_deletion_with_a_wrong_password_changes_nothing(
    client, db_session, monkeypatch
):
    _quiet_gdpr_mail(monkeypatch)
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/data-request",
        json={"request_type": "deletion", "password": "Wr0ngPassword"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Incorrect password"
    stored = _stored_user(db_session)
    assert stored.is_active is True
    assert stored.data_deletion_requested is False


def test_data_request_export_needs_no_password(client, monkeypatch):
    # Reading one's own data is not destructive; the bearer token is enough.
    _quiet_gdpr_mail(monkeypatch)
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/data-request",
        json={"request_type": "export"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200


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
    asyncio.run(blacklist_token(token, expires_in=3600))

    response = client.get(f"{LEGAL_PREFIX}/consent-status", headers=_auth_headers(token))

    assert response.status_code == 401


def test_data_retention_rejects_blacklisted_token(client):
    token = _register_and_login(client, _valid_registration_payload())
    asyncio.run(blacklist_token(token, expires_in=3600))

    response = client.get(f"{LEGAL_PREFIX}/data-retention", headers=_auth_headers(token))

    assert response.status_code == 401


def test_update_consent_rejects_blacklisted_token(client):
    token = _register_and_login(client, _valid_registration_payload())
    asyncio.run(blacklist_token(token, expires_in=3600))

    response = client.post(
        f"{LEGAL_PREFIX}/consent",
        json={"marketing_consent": True, "privacy_policy_accepted": True},
        headers=_auth_headers(token),
    )

    assert response.status_code == 401


def test_data_request_rejects_blacklisted_token(client):
    token = _register_and_login(client, _valid_registration_payload())
    asyncio.run(blacklist_token(token, expires_in=3600))

    response = client.post(
        f"{LEGAL_PREFIX}/data-request",
        json={"request_type": "export"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 401


# --- Regression: the routes that change the user must persist the change ----
#
# POST /legal/consent and POST /legal/data-request (deletion) mutate the
# user. current_user is a detached snapshot (get_current_user expunges it),
# so these routes load the row again in the session they share with the auth
# dependency (both depend on database.get_async_db) and commit that session.
# The tests below check what is actually stored, through a separate session
# and through later requests - the response alone would look right even if
# nothing had been saved.


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
        json={"request_type": "deletion", "password": _valid_registration_payload()["password"]},
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
    # route's lookup is the only AsyncSession.get() of the request; auth
    # reads the user with a select).
    from sqlalchemy.ext.asyncio import AsyncSession

    token = _register_and_login(client, _valid_registration_payload())

    async def _gone(self, *args, **kwargs):
        return None

    monkeypatch.setattr(AsyncSession, "get", _gone)

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


# --- More of the legal surface ----------------------------------------------


def test_the_policies_name_the_controller_and_how_to_reach_the_dpo(client):
    from app.core.config import settings

    privacy = client.get(f"{LEGAL_PREFIX}/privacy-policy").json()["data"]["content"]
    terms = client.get(f"{LEGAL_PREFIX}/terms-of-service").json()["data"]["content"]
    cookies = client.get(f"{LEGAL_PREFIX}/cookie-policy").json()["data"]["content"]

    assert settings.DPO_EMAIL in privacy and settings.COMPANY_ADDRESS in privacy
    assert str(settings.USER_DATA_RETENTION_DAYS) in privacy
    assert settings.COMPANY_VAT in terms
    for cookie in settings.ESSENTIAL_COOKIES:
        assert cookie in cookies
    # Nothing left unfilled.
    for content in (privacy, terms, cookies):
        assert "{" not in content and "}" not in content


def test_a_consent_change_through_legal_is_visible_through_users(client):
    token = _register_and_login(client, _valid_registration_payload())

    client.post(
        f"{LEGAL_PREFIX}/consent",
        json={"marketing_consent": True, "privacy_policy_accepted": True},
        headers=_auth_headers(token),
    )
    status_after = client.get(f"{LEGAL_PREFIX}/consent-status", headers=_auth_headers(token))
    export = client.get(f"{USERS_PREFIX}/data/export", headers=_auth_headers(token))

    assert status_after.json()["data"]["marketing_consent"] is True
    assert export.json()["data"]["personal_data"]["marketing_consent"] is True
    # One history, whichever route wrote it.
    assert [c["type"] for c in export.json()["data"]["consents"]][-1] == "marketing"


def test_a_consent_update_with_a_missing_field_is_422(client):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/consent", json={"marketing_consent": True}, headers=_auth_headers(token)
    )

    assert response.status_code == 422


def test_a_deletion_request_with_the_password_sends_both_mails(client, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.services.email.send_gdpr_request_received",
        lambda to_email, kind, request_id: sent.append(("received", kind)),
    )
    monkeypatch.setattr(
        "app.services.email.send_gdpr_deletion_confirmation",
        lambda to_email, request_id: sent.append(("confirmed", "deletion")),
    )
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/data-request",
        json={"request_type": "deletion", "password": _valid_registration_payload()["password"],
              "reason": "moving on"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert sent == [("received", "deletion"), ("confirmed", "deletion")]


def test_a_password_is_not_needed_for_an_unknown_request_type_to_be_refused(client):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{LEGAL_PREFIX}/data-request",
        json={"request_type": "rectification", "password": "anything"},
        headers=_auth_headers(token),
    )

    assert response.status_code == 400
