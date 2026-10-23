import asyncio
from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.core import database
from app.core.clock import utc_now
from app.models.user import User
from app.schemas.legal import ConsentUpdate
from app.services.gdpr import GDPRService

# The write methods take an AsyncSession. These tests open one from
# database.AsyncSessionLocal, which tests/conftest.py points at the test
# database (aiosqlite file / PostgreSQL), and run one scenario per
# asyncio.run(): the suite has no async test plugin, and the conftest async
# engine uses NullPool so a connection never outlives its loop.


def _run(scenario):
    """Run `async def scenario(session)` on a fresh loop with its own session."""

    async def runner():
        async with database.AsyncSessionLocal() as session:
            return await scenario(session)

    return asyncio.run(runner())


async def _make_user(session, email="gdpr.user@example.com", **overrides):
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
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


def test_update_user_consent_updates_fields_and_history():
    async def scenario(session):
        user = await _make_user(session)
        result = await GDPRService(session).update_user_consent(
            user=user,
            consent_update=ConsentUpdate(marketing_consent=True, privacy_policy_accepted=True),
            ip_address="1.2.3.4",
            user_agent="pytest",
        )
        return result, user.marketing_consent, list(user.consent_history)

    result, marketing, history = _run(scenario)

    assert result == {"marketing": True, "privacy_policy": True}
    assert marketing is True
    assert len(history) == 1
    assert history[0]["type"] == "marketing"


def test_update_user_consent_no_changes_adds_no_history_entry():
    async def scenario(session):
        user = await _make_user(session, marketing_consent=True, privacy_policy_accepted=True)
        await GDPRService(session).update_user_consent(
            user=user,
            consent_update=ConsentUpdate(marketing_consent=True, privacy_policy_accepted=True),
        )
        return user.consent_history

    assert _run(scenario) == []


def test_process_data_export_returns_request_id_and_sends_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.services.email.send_gdpr_request_received",
        lambda *a, **k: sent.append("received"),
    )
    monkeypatch.setattr(
        "app.services.email.send_gdpr_export_email",
        lambda *a, **k: sent.append("export"),
    )

    async def scenario(session):
        user = await _make_user(session)
        return user.id, await GDPRService(session).process_data_export(user)

    user_id, request_id = _run(scenario)

    assert request_id.startswith(f"export_{user_id}_")
    assert sent == ["received", "export"]


def test_process_data_deletion_deactivates_user_and_sends_emails(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.services.email.send_gdpr_request_received",
        lambda *a, **k: sent.append("received"),
    )
    monkeypatch.setattr(
        "app.services.email.send_gdpr_deletion_confirmation",
        lambda *a, **k: sent.append("confirmed"),
    )

    async def scenario(session):
        user = await _make_user(session)
        request_id = await GDPRService(session).process_data_deletion(user)
        return user.id, request_id, user.is_active, user.data_deletion_requested

    user_id, request_id, is_active, requested = _run(scenario)

    assert request_id.startswith(f"deletion_{user_id}_")
    assert is_active is False
    assert requested is True
    assert sent == ["received", "confirmed"]


# The pure methods need no session at all: the service is built without one,
# as the consent-status and data-retention routes do.


def test_get_consent_status_returns_current_state():
    user = User(
        email="a@example.com",
        hashed_password="h",
        marketing_consent=True,
        privacy_policy_accepted=True,
        consent_history=[],
    )

    status = GDPRService().get_consent_status(user)

    assert status["marketing_consent"] is True
    assert status["privacy_policy_accepted"] is True
    assert status["consent_history"] == []


def test_validate_retention_period_true_when_recent():
    assert GDPRService().validate_retention_period(User(created_at=utc_now())) is True


def test_validate_retention_period_false_when_expired():
    user = User(created_at=utc_now() - timedelta(days=1000), data_retention_period=365)

    assert GDPRService().validate_retention_period(user) is False


def test_validate_retention_period_true_when_no_created_at():
    assert GDPRService().validate_retention_period(User(created_at=None)) is True


# --- failure paths: nothing half-done, and a clear 500 ----------------------


def test_withdrawing_privacy_policy_acceptance_is_recorded_with_its_date():
    async def scenario(session):
        user = await _make_user(session)
        result = await GDPRService(session).update_user_consent(
            user=user,
            consent_update=ConsentUpdate(marketing_consent=False, privacy_policy_accepted=False),
            ip_address="1.2.3.4",
        )
        return result, user.privacy_policy_accepted_date, list(user.consent_history)

    result, accepted_date, history = _run(scenario)

    assert result == {"marketing": False, "privacy_policy": False}
    assert accepted_date is not None
    assert [(e["type"], e["granted"], e["ip_address"]) for e in history] == [
        ("privacy_policy", False, "1.2.3.4"),
    ]


def _commit_fails(monkeypatch, session):
    """Make the session's commit raise; return the record of rollbacks."""
    rolled_back = []
    original_rollback = session.rollback

    async def broken_commit():
        raise RuntimeError("database gone")

    async def recording_rollback():
        rolled_back.append(True)
        await original_rollback()

    monkeypatch.setattr(session, "commit", broken_commit)
    monkeypatch.setattr(session, "rollback", recording_rollback)
    return rolled_back


def test_a_consent_update_that_cannot_be_stored_is_rolled_back_and_a_500(monkeypatch):
    async def scenario(session):
        user = await _make_user(session)
        rolled_back = _commit_fails(monkeypatch, session)
        with pytest.raises(HTTPException) as error:
            await GDPRService(session).update_user_consent(
                user=user,
                consent_update=ConsentUpdate(marketing_consent=True, privacy_policy_accepted=True),
            )
        return error.value, rolled_back

    error, rolled_back = _run(scenario)

    assert error.status_code == 500
    assert error.detail == "Failed to update consent preferences"
    assert rolled_back == [True]


def test_an_export_request_whose_mail_fails_is_a_500(monkeypatch):
    def broken(*args):
        raise OSError("smtp down")

    monkeypatch.setattr("app.services.email.send_gdpr_request_received", broken)

    async def scenario(session):
        user = await _make_user(session)
        with pytest.raises(HTTPException) as error:
            await GDPRService(session).process_data_export(user)
        return error.value

    error = _run(scenario)

    assert error.status_code == 500
    assert error.detail == "Failed to process data export request"


def test_a_deletion_request_that_cannot_be_stored_leaves_the_account_as_it_was(monkeypatch):
    monkeypatch.setattr("app.services.email.send_gdpr_request_received", lambda *a: True)

    async def scenario(session):
        user = await _make_user(session)
        user_id = user.id
        rolled_back = _commit_fails(monkeypatch, session)
        with pytest.raises(HTTPException) as error:
            await GDPRService(session).process_data_deletion(user)
        # The rollback expired the user; a fresh read shows what is stored.
        stored = await session.get(User, user_id, populate_existing=True)
        return error.value, rolled_back, stored.is_active, stored.data_deletion_requested

    error, rolled_back, is_active, requested = _run(scenario)

    assert error.status_code == 500
    assert error.detail == "Failed to process deletion request"
    assert rolled_back == [True]
    assert is_active is True
    assert requested is False
