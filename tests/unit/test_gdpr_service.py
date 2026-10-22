import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.core.clock import utc_now
from app.models.user import User
from app.schemas.legal import ConsentUpdate
from app.services.gdpr import GDPRService


def _make_user(db_session, email="gdpr.user@example.com", **overrides):
    defaults = dict(
        email=email,
        hashed_password="hashed",
        full_name="GDPR User",
        gdpr_consent=True,
        privacy_policy_accepted=True,
        marketing_consent=False,
        consent_history=[],
        is_active=True,
        created_at=utc_now(),
    )
    defaults.update(overrides)
    user = User(**defaults)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def test_update_user_consent_updates_fields_and_history(db_session, monkeypatch):
    user = _make_user(db_session)
    service = GDPRService(db_session)

    result = service.update_user_consent(
        user=user,
        consent_update=ConsentUpdate(marketing_consent=True, privacy_policy_accepted=True),
        ip_address="1.2.3.4",
        user_agent="pytest",
    )

    assert result == {"marketing": True, "privacy_policy": True}
    assert user.marketing_consent is True
    assert len(user.consent_history) == 1
    assert user.consent_history[0]["type"] == "marketing"


def test_update_user_consent_no_changes_adds_no_history_entry(db_session):
    user = _make_user(db_session, marketing_consent=True, privacy_policy_accepted=True)
    service = GDPRService(db_session)

    service.update_user_consent(
        user=user,
        consent_update=ConsentUpdate(marketing_consent=True, privacy_policy_accepted=True),
    )

    assert user.consent_history == []


def test_process_data_export_returns_request_id_and_sends_emails(db_session, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.services.email.send_gdpr_request_received",
        lambda *a, **k: sent.append("received"),
    )
    monkeypatch.setattr(
        "app.services.email.send_gdpr_export_email",
        lambda *a, **k: sent.append("export"),
    )
    user = _make_user(db_session)
    service = GDPRService(db_session)

    request_id = asyncio.run(service.process_data_export(user))

    assert request_id.startswith(f"export_{user.id}_")
    assert sent == ["received", "export"]


def test_process_data_deletion_deactivates_user_and_sends_emails(db_session, monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.services.email.send_gdpr_request_received",
        lambda *a, **k: sent.append("received"),
    )
    monkeypatch.setattr(
        "app.services.email.send_gdpr_deletion_confirmation",
        lambda *a, **k: sent.append("confirmed"),
    )
    user = _make_user(db_session)
    service = GDPRService(db_session)

    request_id = asyncio.run(service.process_data_deletion(user))

    assert request_id.startswith(f"deletion_{user.id}_")
    assert user.is_active is False
    assert user.data_deletion_requested is True
    assert sent == ["received", "confirmed"]


def test_get_consent_status_returns_current_state(db_session):
    user = _make_user(db_session, marketing_consent=True)
    service = GDPRService(db_session)

    status = service.get_consent_status(user)

    assert status["marketing_consent"] is True
    assert status["privacy_policy_accepted"] is True
    assert status["consent_history"] == []


def test_validate_retention_period_true_when_recent(db_session):
    user = _make_user(db_session, created_at=utc_now())
    service = GDPRService(db_session)

    assert service.validate_retention_period(user) is True


def test_validate_retention_period_false_when_expired(db_session):
    user = _make_user(
        db_session,
        created_at=utc_now() - timedelta(days=1000),
        data_retention_period=365,
    )
    service = GDPRService(db_session)

    assert service.validate_retention_period(user) is False


def test_validate_retention_period_true_when_no_created_at(db_session):
    user = _make_user(db_session)
    user.created_at = None
    service = GDPRService(db_session)

    assert service.validate_retention_period(user) is True


# --- failure paths: nothing half-done, and a clear 500 ----------------------


def test_withdrawing_privacy_policy_acceptance_is_recorded_with_its_date(db_session):
    user = _make_user(db_session)
    service = GDPRService(db_session)

    result = service.update_user_consent(
        user=user,
        consent_update=ConsentUpdate(marketing_consent=False, privacy_policy_accepted=False),
        ip_address="1.2.3.4",
    )

    assert result == {"marketing": False, "privacy_policy": False}
    assert user.privacy_policy_accepted_date is not None
    assert [(e["type"], e["granted"], e["ip_address"]) for e in user.consent_history] == [
        ("privacy_policy", False, "1.2.3.4"),
    ]


def _commit_fails(monkeypatch, db_session):
    """Make the session's commit raise; return the record of rollbacks."""
    rolled_back = []
    original_rollback = db_session.rollback

    def broken_commit():
        raise RuntimeError("database gone")

    def recording_rollback():
        rolled_back.append(True)
        original_rollback()

    monkeypatch.setattr(db_session, "commit", broken_commit)
    monkeypatch.setattr(db_session, "rollback", recording_rollback)
    return rolled_back


def test_a_consent_update_that_cannot_be_stored_is_rolled_back_and_a_500(db_session, monkeypatch):
    user = _make_user(db_session)
    rolled_back = _commit_fails(monkeypatch, db_session)

    with pytest.raises(HTTPException) as error:
        GDPRService(db_session).update_user_consent(
            user=user,
            consent_update=ConsentUpdate(marketing_consent=True, privacy_policy_accepted=True),
        )

    assert error.value.status_code == 500
    assert error.value.detail == "Failed to update consent preferences"
    assert rolled_back == [True]


def test_an_export_request_whose_mail_fails_is_a_500(db_session, monkeypatch):
    def broken(*args):
        raise OSError("smtp down")

    monkeypatch.setattr("app.services.email.send_gdpr_request_received", broken)

    with pytest.raises(HTTPException) as error:
        asyncio.run(GDPRService(db_session).process_data_export(_make_user(db_session)))

    assert error.value.status_code == 500
    assert error.value.detail == "Failed to process data export request"


def test_a_deletion_request_that_cannot_be_stored_leaves_the_account_as_it_was(
    db_session, monkeypatch
):
    user = _make_user(db_session)
    monkeypatch.setattr("app.services.email.send_gdpr_request_received", lambda *a: True)
    rolled_back = _commit_fails(monkeypatch, db_session)

    with pytest.raises(HTTPException) as error:
        asyncio.run(GDPRService(db_session).process_data_deletion(user))

    assert error.value.status_code == 500
    assert error.value.detail == "Failed to process deletion request"
    assert rolled_back == [True]
    db_session.expire_all()
    stored = db_session.get(User, user.id)
    assert stored.is_active is True
    assert stored.data_deletion_requested is False
