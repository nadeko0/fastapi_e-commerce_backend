"""Reproductions of the confirmed findings of the v4.0.3 pre-release audit.

Source: .cloud_audit/04_validation_phase2.md (canonical IDs) and the reports
.cloud_audit/02a..02d. Every test here asserts the CURRENT, faulty behaviour:
a pass means the defect is still present. When a finding is fixed its test
fails; invert the assertion as its docstring says and keep it as the
regression test.

The PostgreSQL-only reproductions (DB-01 deadlock, GDPR-02 idle-in-transaction
timeout) live in test_postgres_cloud_audit.py next to this file, which
tests/conftest.py marks db_sensitive by its name.
"""

import ast
import dataclasses
import json
import os
import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

import app.api.v1.users as users_mod
import app.core.security as sec
from app import tasks
from app.core.clock import utc_now
from app.core.config import Settings, settings
from app.core.rate_limit import _ROUTE_LIMITS
from app.core.security import get_password_hash, invalidate_tokens_issued_before_now
from app.main import app
from app.models.address import Address
from app.models.order import Order
from app.models.payment import Payment, WebhookEvent
from app.models.user import User
from app.services import gdpr as gdpr_mod
from app.services import stripe_events
from app.services.email import messages as email_messages
from app.services.payment.exceptions import IdempotencyError
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import PaymentIntentStatus
from app.services.reconciliation import PENDING_INTENT_ID_PREFIX, reconcile_payments

# The integration helpers (Shop, the webhook delivery) are imported by bare
# name, as the tests in tests/integration do; pytest puts that directory on
# sys.path only for tests collected from it.
_INTEGRATION = Path(__file__).resolve().parents[1] / "integration"
if str(_INTEGRATION) not in sys.path:
    sys.path.insert(0, str(_INTEGRATION))

from conftest import IS_POSTGRES, TestingSessionLocal, async_engine, engine  # noqa: E402
from payment_support import PAY_3DS, PAY_OK, Shop, order_state, reset_provider  # noqa: E402
from test_admin import (  # noqa: E402
    ADMIN_PREFIX,
    _auth_headers,
    _login_as,
    _make_admin,
    _make_category,
    _valid_product_payload,
)

REPO = Path(__file__).resolve().parents[2]
USERS = "/api/v1/users"
LEGAL = "/api/v1/legal"
CART = "/api/v1/cart"
PW = "Zx9!qwerty-unique"
BUYER_PW = "Str0ngPass1"  # Shop.buyer()'s password
MERGE_PATCH = {"Content-Type": "application/merge-patch+json"}


# -- helpers -----------------------------------------------------------------


def _placed_order(client, db_session, stock=1):
    reset_provider()
    shop = Shop(client, db_session)
    product = shop.product(stock=stock)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product).status_code == 200
    placed = shop.place_order(buyer)
    assert placed.status_code == 201, placed.text
    return shop, product, buyer, placed.json()


def _register_and_login(client, email, **extra):
    body = {"email": email, "password": PW, "gdpr_consent": True,
            "privacy_policy_accepted": True, **extra}
    assert client.post(f"{USERS}/register", json=body).status_code == 202
    return client.post(f"{USERS}/login", data={"username": email, "password": PW}).json()


class _Mailbox:
    """Stands in for the email provider: what the real send_* functions
    would have put on the wire."""

    def __init__(self):
        self.sent = []

    def send(self, to_email, subject, html_content):
        self.sent.append((to_email, subject, html_content))
        return True


@pytest.fixture
def mailbox(monkeypatch):
    box = _Mailbox()
    monkeypatch.setattr("app.services.email.get_email_provider", lambda: box)
    return box


def _mark_intent_succeeded(payment_intent_id):
    """Real Stripe reports a late-approved 3DS intent as succeeded; the mock
    keeps it requires_action. Mirror Stripe."""
    current = StripePaymentProvider._intents_by_id[payment_intent_id]
    changes = {"status": PaymentIntentStatus.SUCCEEDED, "amount_received": current.amount}
    StripePaymentProvider._intents_by_id[payment_intent_id] = (
        dataclasses.replace(current, **changes) if dataclasses.is_dataclass(current)
        else current.model_copy(update=changes)
    )


# == High ======================================================================


def test_PAY01_late_3ds_charge_on_expired_order_is_unrefundable_and_unreported(
    client, db_session, tasks_db
):
    """PAY-01 (2A-01), High. app/tasks.py:479-499 (expiry ignores in-flight
    attempts); app/api/v1/orders.py:765-767 (late success only flagged),
    orders.py:1540/1558 (/refund refuses it).

    Scenario: the buyer is in 3D Secure when the unpaid order expires; the
    order is cancelled and restocked, the 3DS approval then charges the card,
    the charge is only flagged, /refund answers 409 and reconciliation opens
    no discrepancy.

    When fixed, invert the assertion: expiry must skip (or expire at Stripe)
    an order with an in-flight attempt, or the late charge must be refundable
    (refund 200) / surfaced by reconciliation (opened >= 1).
    """
    shop, product, buyer, order = _placed_order(client, db_session)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    assert shop.expire_unpaid(order["id"]) == 1          # in-flight attempt ignored
    assert shop.stock(product) == 1                      # restocked and resellable
    late = shop.intent_event("payment_intent.succeeded", shop.payments(order["id"])[0])
    assert late.json()["requires_manual_review"] is True
    assert order_state(shop, order["id"]) == ("cancelled", "pending")
    assert shop.refund(shop.admin(), order["id"]).status_code == 409  # no in-app refund path

    _mark_intent_succeeded(shop.payments(order["id"])[0].payment_intent_id)
    result = reconcile_payments(db_session, StripePaymentProvider(),
                                now=utc_now() + timedelta(hours=2))
    assert result.opened == 0                            # reconciliation is silent


def test_PAY01_customer_cancel_ignores_in_flight_3ds_attempt(client, db_session):
    """PAY-01 (2B-03), High. app/api/v1/orders.py:557 (cancel_own_order checks
    is_unpaid_and_new only, no in-flight check).

    Scenario: the buyer cancels the order while its 3DS attempt is pending;
    the approval lands afterwards, the money is taken, the charge is only
    flagged and /refund refuses it.

    When fixed, invert the assertion: the cancel must answer 409 while an
    attempt is in flight (or cancel the intent at Stripe), or the late charge
    must be refundable through /refund.
    """
    shop, _product, buyer, order = _placed_order(client, db_session)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    assert shop.cancel(buyer, order["id"]).status_code == 200   # accepted mid-3DS
    late = shop.intent_event("payment_intent.succeeded", shop.payments(order["id"])[0])
    assert late.json()["requires_manual_review"] is True
    assert order_state(shop, order["id"]) == ("cancelled", "pending")
    assert shop.refund(shop.admin(), order["id"]).status_code == 409


def test_PAY01_second_checkout_session_after_stale_window_double_charges(client, db_session):
    """PAY-01 (2A-04), High (merged). app/api/v1/orders.py:919-926
    (_guard_no_attempt_in_flight stops blocking after
    PAYMENT_ATTEMPT_STALE_AFTER_MINUTES); live_stripe_provider.py:347-367 (no
    expires_at); sibling sessions are never expired.

    Scenario: tab A opens a Checkout Session, the buyer waits more than 5
    minutes, tab B opens a second one; both get paid. The first charge is
    only flagged for manual review and cannot be refunded in-app.

    When fixed, invert the assertion: the second checkout-session must answer
    409 while the first session can still be paid, or the first session must
    be expired once the second succeeds.
    """
    shop, _product, buyer, order = _placed_order(client, db_session)
    assert shop.checkout_session(buyer, order["id"]).status_code == 200
    first = shop.payments(order["id"])[0]
    first.created_at = utc_now() - timedelta(
        minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES + 1)
    db_session.commit()

    second_session = shop.checkout_session(buyer, order["id"])
    assert second_session.status_code == 200, second_session.text   # not 409
    first, second = shop.payments(order["id"])

    done = shop.session_event("checkout.session.completed", second, payment_intent="pi_cs_B")
    assert done.json()["applied"] is True
    late = shop.session_event("checkout.session.completed", first, payment_intent="pi_cs_A")
    assert late.json()["requires_manual_review"] is True
    assert [p.status for p in shop.payments(order["id"])] == ["succeeded", "succeeded"]
    assert [p.requires_manual_review for p in shop.payments(order["id"])] == [True, False]
    assert order_state(shop, order["id"]) == ("confirmed", "paid")


# == Medium ====================================================================


def test_PAY02_checkout_session_without_webhook_is_skipped_by_reconciliation(client, db_session):
    """PAY-02 (2A-02), Medium. app/services/reconciliation.py:288-291 (rows
    whose payment_intent_id is still `pending-...` are skipped;
    retrieve_checkout_session is never called).

    Scenario: a Checkout Session is paid but its completion webhook is lost;
    reconciliation never asks Stripe about it.

    When fixed, invert the assertion: checked == 1 and skipped == 0 (the
    session is retrieved from the provider).
    """
    shop, _product, buyer, order = _placed_order(client, db_session)
    assert shop.checkout_session(buyer, order["id"]).status_code == 200
    assert shop.payments(order["id"])[0].payment_intent_id.startswith("pending-cs-")
    result = reconcile_payments(db_session, StripePaymentProvider(),
                                now=utc_now() + timedelta(hours=2))
    assert (result.checked, result.skipped) == (0, 1)


def test_PAY03_stale_pending_refund_event_regresses_refunded_order_to_paid(client, db_session):
    """PAY-03 (2A-03), Medium. app/services/stripe_events.py:253-257 (any
    non-final refund status overwrites `succeeded`); orders.py:1795-1804
    (_settle_refunds returns the order to PAID).

    Scenario: a full refund succeeds, then a late `refund.created` event
    (status pending) is delivered out of order; the order is PAID again and
    an admin can move it to processing (ship it).

    When fixed, invert the assertion: payment_status stays "refunded" and
    PUT /status processing answers 409.
    """
    shop, _product, buyer, order = _placed_order(client, db_session)
    assert shop.pay(buyer, order["id"], token=PAY_OK).status_code == 200
    admin = shop.admin()
    refund = shop.refund(admin, order["id"])
    assert refund.status_code == 200, refund.text
    assert order_state(shop, order["id"])[1] == "refunded"
    pay = shop.payments(order["id"])[0]
    shop.webhook("refund.created", {
        "id": refund.json()["id"], "object": "refund", "status": "pending",
        "amount": pay.amount, "payment_intent": pay.payment_intent_id,
        "currency": pay.currency.lower(),
    })
    assert order_state(shop, order["id"])[1] == "paid"              # refunded -> paid
    assert shop.set_status(admin, order["id"], "processing").status_code == 200


def test_GDPR01_webhook_event_after_erasure_keeps_pii_forever(db_session):
    """GDPR-01 (2C-01 + 2D-06 last_error), Medium. app/tasks.py:332-342,426-433
    (purges skip anonymised users); redact_users_events runs only at erasure
    (tasks.py:363); no retention job deletes webhook_events.

    Scenario: a refund/dispute event carrying the customer's billing details
    arrives after the account was erased; no later purge ever redacts it.

    When fixed, invert the assertion: the stored payload no longer contains
    the email (redacted at store time or by a recurring purge).
    """
    when = utc_now() - timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 2)
    user = User(email="jane@example.com", hashed_password=get_password_hash("Str0ngPass1"),
                full_name="Jane Roe", is_active=False, data_deletion_requested=True,
                data_deletion_date=when)
    db_session.add(user)
    db_session.commit()
    address = Address(user_id=user.id, street="Main 1", city="Berlin", state="BE",
                      postal_code="10115", country="DE", is_active=True)
    db_session.add(address)
    db_session.commit()
    order = Order(user_id=user.id, shipping_address_id=address.id, total_amount=10)
    db_session.add(order)
    db_session.commit()
    db_session.add(Payment(order_id=order.id, payment_intent_id="pi_late", amount=1000,
                           currency="eur"))
    db_session.commit()

    assert tasks.purge_expired_deletion_requests(db_session) == 1
    payload = json.dumps({"id": "evt_after", "type": "charge.refunded", "created": 1, "data": {
        "object": {
            "id": "ch_1", "object": "charge", "payment_intent": "pi_late",
            "billing_details": {"name": "Jane Roe", "email": "jane@example.com"},
            "metadata": {"order_id": str(order.id)}, "refunds": {"data": []},
        }}})
    stripe_events.store_event(db_session, "evt_after", "charge.refunded", payload)
    assert tasks.purge_expired_deletion_requests(db_session) == 0
    assert tasks.purge_expired_retention(db_session) == 0
    db_session.expire_all()
    row = db_session.query(WebhookEvent).filter_by(event_id="evt_after").one()
    assert "jane@example.com" in row.payload


def test_GDPR02_purge_hashes_bcrypt_inside_one_open_transaction(monkeypatch):
    """GDPR-02 (2D-01 + 2D-C18), Medium. app/tasks.py:346-368 and :370-372
    (_anonymise_account calls get_password_hash for every account of a batch
    of up to 100 while the batch's SELECT transaction stays open; on
    PostgreSQL idle_in_transaction_session_timeout=30s kills it - see
    test_postgres_cloud_audit.py for the failure itself).

    Scenario: three accounts are due; every bcrypt hash runs inside the same
    open transaction, before its single commit.

    When fixed, invert the assertion: no hash runs while the session holds an
    open transaction (hash first, or commit per account / use a cheap marker).
    """
    with TestingSessionLocal() as setup:
        old = utc_now() - timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 2)
        for i in range(3):
            setup.add(User(email=f"due{i}@example.com", hashed_password="x", is_active=False,
                           data_deletion_requested=True, data_deletion_date=old))
        setup.commit()

    db = TestingSessionLocal()
    commits = []
    event.listen(db, "after_commit", lambda session: commits.append(1))
    seen = []
    real_hash = tasks.get_password_hash

    def spying_hash(password):
        seen.append((db.in_transaction(), len(commits)))
        return real_hash(password)

    monkeypatch.setattr(tasks, "get_password_hash", spying_hash)
    try:
        assert tasks.purge_expired_deletion_requests(db) == 3
    finally:
        db.close()
    assert seen == [(True, 0)] * 3          # all hashed in one open transaction
    assert len(commits) == 1


def test_GDPR03_patch_me_changes_marketing_consent_without_history(client, db_session):
    """GDPR-03 (2C-07), Medium. app/api/v1/users.py:540-541 (raw setattr of
    UserUpdate.marketing_consent, schemas/user.py:159); the intended path is
    app/services/gdpr.py:40-49.

    Scenario: PATCH /users/me {"marketing_consent": true} flips the flag with
    no consent_history entry and no marketing_consent_date.

    When fixed, invert the assertion: marketing_consent_date is set and the
    last "marketing" history entry has granted=True (or the field is refused).
    """
    tokens = _register_and_login(client, "c@example.com", marketing_consent=False)
    response = client.patch(f"{USERS}/me", json={"marketing_consent": True},
                            headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert response.status_code == 200
    db_session.expire_all()
    user = db_session.query(User).filter_by(email="c@example.com").one()
    assert user.marketing_consent is True
    assert user.marketing_consent_date is None
    assert [e for e in user.consent_history if e["type"] == "marketing"][-1]["granted"] is False


def test_GDPR04_erasure_redacts_address_of_paid_unshipped_order(client, db_session):
    """GDPR-04 (2C-10), Medium. app/services/gdpr.py:119-147 and
    app/tasks.py:315-397 (erasure with no check for paid-unshipped orders or
    open disputes; addresses redacted after DATA_DELETION_GRACE_PERIOD_DAYS).

    Scenario: a customer with a paid, not yet shipped order asks for erasure;
    one day later the purge redacts the shipping address the warehouse still
    needs.

    When fixed, invert the assertion: the purge defers this account (returns
    0) or the order keeps a usable shipping address snapshot.
    """
    shop, _product, buyer, order = _placed_order(client, db_session)
    assert shop.pay(buyer, order["id"], token=PAY_OK).status_code == 200
    user = db_session.get(User, buyer.user_id)
    user.is_active = False
    user.data_deletion_requested = True
    user.data_deletion_date = utc_now() - timedelta(
        days=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 1)
    db_session.commit()

    assert tasks.purge_expired_deletion_requests(db_session) == 1
    stored = shop.order(order["id"])
    assert order_state(shop, order["id"]) == ("confirmed", "paid")
    assert db_session.get(Address, stored.shipping_address_id).street == "REDACTED"


def test_GDPR05_data_export_contains_no_personal_data(client, db_session, mailbox):
    """GDPR-05 (2C-11), Medium. app/services/gdpr.py:100-109 ("In production,
    the export payload would be assembled here"); messages.py:330 claims
    "here is your personal data export"; SECURITY.md:115 claims a JSON export
    of account, addresses, orders and consents.

    Scenario: an Art. 15 export request mails a "here is your export" message
    that carries none of the user's data, and nothing is recorded.

    When fixed, invert the assertion: the export (or its link target)
    contains the user's name/address/orders and data_export_requests records
    the request.
    """
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    response = client.post(f"{LEGAL}/data-request", json={"request_type": "export"},
                           headers=buyer.headers)
    assert response.status_code == 200
    [(_to, _subject, body)] = [
        m for m in mailbox.sent if "here is your personal data export" in m[2]]
    for personal in ("Buyer One", "1 Main Street", "Metropolis", "+15551234567"):
        assert personal not in body
    db_session.expire_all()
    assert db_session.get(User, buyer.user_id).data_export_requests in ([], None)
    assert "export user account records, addresses, order histories" in (
        REPO / "SECURITY.md").read_text(encoding="utf-8")


def test_AUTH01_login_in_flight_during_password_reset_survives_it(client, db_session, monkeypatch):
    """AUTH-01 (2C-02), Medium. app/api/v1/users.py:332-388 (tokens minted
    with no re-check of the verified hash); reset (users.py:1055-1072) only
    moves the watermark and revokes no refresh families.

    Scenario: an attacker's login with the OLD password is inside bcrypt when
    the victim completes a password reset; the tokens minted afterwards work,
    and so does their refresh chain.

    When fixed, invert the assertion: GET /users/me and /users/refresh answer
    401 for those tokens.
    """
    _register_and_login(client, "r@example.com")
    uid = db_session.query(User).filter_by(email="r@example.com").one().id
    real_verify = users_mod.verify_password_async

    async def verify_then_reset_happens(plain, hashed):
        ok = await real_verify(plain, hashed)
        await invalidate_tokens_issued_before_now(uid)
        db_session.execute(update(User).where(User.id == uid).values(
            hashed_password=get_password_hash("N3w-Pass-phrase!")))
        db_session.commit()
        return ok

    with monkeypatch.context() as m:
        m.setattr(users_mod, "verify_password_async", verify_then_reset_happens)
        tokens = client.post(f"{USERS}/login",
                             data={"username": "r@example.com", "password": PW}).json()
    me = client.get(f"{USERS}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert me.status_code == 200
    refreshed = client.post(f"{USERS}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200


def test_AUTH01_refresh_rotation_in_flight_during_reset_survives_it(client, db_session, monkeypatch):
    """AUTH-01 (2C-03, merged), Medium. app/core/security.py:527-539 (rotation
    path relies on the watermark check only; no family revocation at reset).

    Scenario: a refresh rotation passes the superseded-check, then the reset
    lands; the rotated token keeps refreshing indefinitely.

    When fixed, invert the assertion: the follow-up refresh answers 401.
    """
    tokens = _register_and_login(client, "s@example.com")
    uid = db_session.query(User).filter_by(email="s@example.com").one().id
    real = sec.is_token_superseded

    async def check_then_reset(user_id, iat):
        result = await real(user_id, iat)
        await invalidate_tokens_issued_before_now(uid)
        return result

    with monkeypatch.context() as m:
        m.setattr(sec, "is_token_superseded", check_then_reset)
        rotated = client.post(f"{USERS}/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert rotated.status_code == 200
    again = client.post(f"{USERS}/refresh", json={"refresh_token": rotated.json()["refresh_token"]})
    assert again.status_code == 200


def test_AUTH02_deletion_reauth_is_an_unthrottled_password_oracle(client, db_session, monkeypatch):
    """AUTH-02 (2C-04), Medium. app/core/rate_limit.py:75-87 (no entry for
    /legal/data-request, so RATE_LIMIT_DEFAULT 300/min applies);
    app/api/v1/legal.py:338-343 never calls check_login_attempt.

    Scenario: with a stolen access token, 30 wrong passwords are tried
    against the deletion re-auth with no lockout; the right one then erases
    the account.

    When fixed, invert the assertion: the route is in _ROUTE_LIMITS with an
    auth limit and failures count towards the login lockout (429/423 before
    the correct guess is accepted).
    """
    for name in ("send_gdpr_request_received", "send_gdpr_deletion_confirmation"):
        monkeypatch.setattr(f"app.services.email.{name}", lambda *a, **k: True)
    assert ("POST", "/legal/data-request") not in _ROUTE_LIMITS
    token = _register_and_login(client, "u@example.com")["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    for i in range(30):
        guess = client.post(f"{LEGAL}/data-request", headers=headers,
                            json={"request_type": "deletion", "password": f"guess-{i}"})
        assert guess.status_code == 401
    hit = client.post(f"{LEGAL}/data-request", headers=headers,
                      json={"request_type": "deletion", "password": PW})
    assert hit.status_code == 200


def test_REG01_registration_without_full_name_never_sends_verification_mail(
    client, db_session, monkeypatch, mailbox
):
    """REG-01 (2C-05), Medium. app/services/email/messages.py:207
    (html.escape(full_name) with full_name optional, schemas/user.py:92);
    call site app/api/v1/users.py:212-217.

    Scenario: POST /users/register without full_name answers 202, but the
    welcome/verification mail task crashes, so the account can never verify
    (checkout requires verification, orders.py:186).

    When fixed, invert the assertion: no exception, one verification mail sent.
    """
    errors = []
    real_send = email_messages.send_welcome_email

    def recording_send(*args, **kwargs):
        try:
            return real_send(*args, **kwargs)
        except Exception as exc:  # the background task would die here
            errors.append(exc)
            return False

    monkeypatch.setattr(users_mod, "send_welcome_email", recording_send)
    response = client.post(f"{USERS}/register", json={
        "email": "noname@example.com", "password": PW, "gdpr_consent": True,
        "privacy_policy_accepted": True})
    assert response.status_code == 202
    assert len(errors) == 1 and isinstance(errors[0], AttributeError)
    assert mailbox.sent == []
    db_session.expire_all()
    assert db_session.query(User).filter_by(email="noname@example.com").one().is_email_verified is False


def test_DATA01_address_edit_rewrites_delivered_order(client, db_session):
    """DATA-01 (2C-06), Medium. app/api/v1/users.py:777-778 (address edited in
    place); orders reference it by FK with no snapshot.

    Scenario: after an order was delivered, the customer edits the address;
    the historical order now shows the new street.

    When fixed, invert the assertion: the order still shows "Main 1 st"
    (copy-on-write address or a snapshot on the order).
    """
    token = _register_and_login(client, "d@example.com")["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    address = client.post(f"{USERS}/addresses", headers=headers, json={
        "street": "Main 1 st", "city": "Berlin", "state": "BE", "postal_code": "10115",
        "country": "DE"}).json()
    user = db_session.query(User).filter_by(email="d@example.com").one()
    order = Order(user_id=user.id, shipping_address_id=address["id"], total_amount=10,
                  status="delivered", payment_status="paid")
    db_session.add(order)
    db_session.commit()
    edited = client.patch(f"{USERS}/addresses/{address['id']}", headers=headers,
                          json={"street": "Elsewhere 99", "country": "US"})
    assert edited.status_code == 200
    shown = client.get(f"/api/v1/orders/{order.id}", headers=headers).json()
    assert shown["shipping_address"]["street"] == "Elsewhere 99"


# == Low =======================================================================


def test_2A05_pending_rows_consume_the_reconciliation_cap(client, db_session, monkeypatch):
    """2A-05, Low. app/services/reconciliation.py:278-291 (newest-first
    LIMIT RECONCILIATION_MAX_PAYMENTS, and `pending-` rows are skipped only
    after they have taken a slot of the cap).

    Scenario: cap 2, one real payment and two newer abandoned Checkout
    placeholders; the real payment is never compared.

    When fixed, invert the assertion: the real payment is checked
    (checked == 1), e.g. placeholders filtered out in SQL or a cursor.
    """
    monkeypatch.setattr(settings, "RECONCILIATION_MAX_PAYMENTS", 2)
    _shop, _product, _buyer, order = _placed_order(client, db_session)
    created = utc_now() - timedelta(hours=1)
    db_session.add(Payment(order_id=order["id"], payment_intent_id="pi_audit_real",
                           amount=100, currency="USD", status="succeeded", created_at=created))
    db_session.commit()
    for i in range(2):
        db_session.add(Payment(order_id=order["id"],
                               payment_intent_id=f"{PENDING_INTENT_ID_PREFIX}cs-audit-{i}",
                               amount=100, currency="USD", created_at=created))
        db_session.commit()
    result = reconcile_payments(db_session, StripePaymentProvider(), now=utc_now())
    assert (result.checked, result.skipped, result.truncated) == (0, 2, True)


def test_2A06_pay_answers_paid_when_success_was_not_applied(client, db_session, monkeypatch):
    """2A-06, Low. app/api/v1/orders.py:1292-1307 (_apply_payment_success's
    False return is discarded; the response is always PAID).

    Scenario: the order is cancelled while Stripe confirms the intent; /pay
    still answers status "paid" although the order is cancelled and the
    charge was only flagged.

    When fixed, invert the assertion: /pay answers 409 (or a non-paid status)
    when the success was not applied.
    """
    shop, _product, buyer, order = _placed_order(client, db_session)
    real_confirm = StripePaymentProvider.confirm_payment_intent

    def confirm_then_cancel(self, *args, **kwargs):
        intent = real_confirm(self, *args, **kwargs)
        with TestingSessionLocal() as other:
            other.get(Order, order["id"]).status = "cancelled"
            other.commit()
        return intent

    monkeypatch.setattr(StripePaymentProvider, "confirm_payment_intent", confirm_then_cancel)
    paid = shop.pay(buyer, order["id"], token=PAY_OK)
    assert paid.status_code == 200
    assert paid.json()["status"] == "paid"
    assert order_state(shop, order["id"]) == ("cancelled", "pending")
    assert shop.payments(order["id"])[0].requires_manual_review is True


def test_2A07_orphaned_processing_attempt_answers_409_forever(client, db_session, monkeypatch):
    """2A-07, Low. app/api/v1/orders.py:695-704 (_replay_payment answers 409
    "still being processed" for any `processing` row); orders.py:1208 leaves a
    fresh attempt `processing` after an IdempotencyError.

    Scenario: an attempt is left `processing` with no request alive; a day
    later the same Idempotency-Key still gets 409 + Retry-After.

    When fixed, invert the assertion: a stale `processing` attempt is retried
    like `unknown` (or answered with a terminal status), not 409.
    """
    shop, _product, buyer, order = _placed_order(client, db_session)

    def refuse(self, *args, **kwargs):
        raise IdempotencyError("refused")

    with monkeypatch.context() as m:
        m.setattr(StripePaymentProvider, "create_payment_intent", refuse)
        assert shop.pay(buyer, order["id"], key="stuck-key-0001").status_code == 409
    [attempt] = shop.payments(order["id"])
    assert attempt.status == "processing"
    attempt.created_at = utc_now() - timedelta(days=1)
    db_session.commit()

    again = shop.pay(buyer, order["id"], key="stuck-key-0001")
    assert again.status_code == 409
    assert "Retry-After" in again.headers
    assert shop.payments(order["id"])[0].status == "processing"


def test_2B02_sync_engine_hook_never_sees_route_stock_updates(client, db_session):
    """2B-02, Low. tests/integration/test_postgres_only.py:177 listens on the
    sync `engine`, but routes run on `async_engine` (tests/conftest.py).

    Scenario: a checkout decrements stock; a listener on the sync engine sees
    0 stock UPDATEs, one on async_engine.sync_engine sees it.

    When fixed (the PG lock tests hook async_engine.sync_engine), this test
    documents the engine split; delete it or point it at the fixed helper.
    """
    shop = Shop(client, db_session)
    product = shop.product(stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product).status_code == 200
    seen = {"sync": 0, "async": 0}

    def spy(name):
        def _spy(conn, cursor, statement, *rest):
            if statement.startswith("UPDATE products SET stock_quantity"):
                seen[name] += 1
        return _spy

    sync_spy, async_spy = spy("sync"), spy("async")
    event.listen(engine, "after_cursor_execute", sync_spy)
    event.listen(async_engine.sync_engine, "after_cursor_execute", async_spy)
    try:
        assert shop.place_order(buyer).status_code == 201
    finally:
        event.remove(engine, "after_cursor_execute", sync_spy)
        event.remove(async_engine.sync_engine, "after_cursor_execute", async_spy)
    assert seen == {"sync": 0, "async": 1}


def _admin_headers(client, db_session):
    admin = _make_admin(db_session)
    return _auth_headers(_login_as(client, admin.email, "AdminPass1"))


def _char(name, value):
    return {"name": name, "value": value, "unit": None, "filterable": True}


def test_2B04_merge_patch_replaces_nested_characteristics(client, db_session):
    """2B-04, Low. app/api/v1/admin.py:286-287 (top-level setattr; labelled
    RFC 7396 but nested objects are replaced, and a null member is refused).

    Scenario: PATCH characteristics {"color": ...} drops the "size" entry;
    {"color": null} answers 422 instead of removing the member.

    When fixed, invert the assertion: "size" survives and the null removes
    "color" with 200 (or the RFC 7396 label is dropped from the docs).
    """
    headers = _admin_headers(client, db_session)
    category = _make_category(db_session)
    body = _valid_product_payload(category.id)
    body["characteristics"] = {"color": _char("Color", "red"), "size": _char("Size", "M")}
    product = client.post(f"{ADMIN_PREFIX}/products", json=body, headers=headers).json()
    patched = client.patch(f"{ADMIN_PREFIX}/products/{product['id']}",
                           json={"characteristics": {"color": _char("Color", "blue")}},
                           headers={**headers, **MERGE_PATCH})
    assert patched.status_code == 200
    assert "size" not in patched.json()["characteristics"]
    nulled = client.patch(f"{ADMIN_PREFIX}/products/{product['id']}",
                          json={"characteristics": {"color": None}},
                          headers={**headers, **MERGE_PATCH})
    assert nulled.status_code == 422


def test_2B04_merge_patch_stores_null_in_variant_attributes(client, db_session):
    """2B-04, Low. app/api/v1/admin.py:831-832 (variant attributes replaced
    wholesale; a JSON null is stored as a value instead of deleting the key).

    Scenario: PATCH attributes {"size": null} on {"size": "M", "color": "red"}
    leaves {"size": null}.

    When fixed, invert the assertion: attributes == {"color": "red"}.
    """
    headers = _admin_headers(client, db_session)
    category = _make_category(db_session)
    product = client.post(f"{ADMIN_PREFIX}/products", json=_valid_product_payload(category.id),
                          headers=headers).json()
    variant = client.post(f"{ADMIN_PREFIX}/products/{product['id']}/variants",
                          json={"sku": "SKU-1", "attributes": {"size": "M", "color": "red"},
                                "stock_quantity": 3}, headers=headers).json()
    patched = client.patch(f"{ADMIN_PREFIX}/products/{product['id']}/variants/{variant['id']}",
                           json={"attributes": {"size": None}}, headers={**headers, **MERGE_PATCH})
    assert patched.status_code == 200
    assert patched.json()["attributes"] == {"size": None}


def test_2B07_order_total_is_not_bounded_by_its_numeric_column(client, db_session):
    """2B-07, Low. app/api/v1/orders.py:366-368 (total_amount accumulated with
    no bound against Numeric(10,2), models/base.py:36).

    Scenario: 100,001 units at 1,000.00 make a 100,001,000.00 total. On
    PostgreSQL the commit raises NumericValueOutOfRange -> 500; SQLite does
    not enforce the precision, so the out-of-range total is stored (201).

    When fixed, invert the assertion: the order is refused with a 4xx before
    the commit.
    """
    shop = Shop(client, db_session)
    product = shop.product(price="1000.00", stock=200_000)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, quantity=100_001).status_code == 200
    placed = shop.place_order(buyer)
    if IS_POSTGRES:
        assert placed.status_code == 500, placed.text
    else:
        assert placed.status_code == 201, placed.text
        assert Decimal(placed.json()["total_amount"]) > Decimal("99999999.99")


def test_2B09_guest_cart_merge_drops_lines_silently(client, db_session, monkeypatch):
    """2B-09, Low. app/schemas/common.py:496-499 (left_out counted),
    app/services/redis_codec.py:288-293 (only logged), redis_async.py:339-341
    (guest cart deleted).

    Scenario: CART_MAX_LINES=2, the account cart has 1 line, the guest cart
    2 distinct lines; after login the cart has 2 lines, one guest line is
    gone, and the login response says nothing.

    When fixed, invert the assertion: the unmerged line is kept (guest key)
    or reported to the client.
    """
    monkeypatch.setattr(settings, "CART_MAX_LINES", 2)
    shop = Shop(client, db_session)
    account_line, guest_a, guest_b = (shop.product(stock=10, name=f"P{i}") for i in range(3))
    _register_and_login(client, "merge@example.com")
    visitor = TestClient(app, base_url="https://testserver")
    login = visitor.post(f"{USERS}/login", data={"username": "merge@example.com", "password": PW})
    token = login.json()["access_token"]
    auth = {"Authorization": f"Bearer {token}"}
    assert visitor.post(f"{CART}/items", json={"product_id": account_line.id},
                        headers=auth).status_code == 200
    guest = TestClient(app, base_url="https://testserver")
    for product in (guest_a, guest_b):
        assert guest.post(f"{CART}/items", json={"product_id": product.id}).status_code == 200

    merged_login = guest.post(f"{USERS}/login",
                              data={"username": "merge@example.com", "password": PW})
    assert merged_login.status_code == 200
    assert "left_out" not in merged_login.text
    new_auth = {"Authorization": f"Bearer {merged_login.json()['access_token']}"}
    cart = guest.get(CART, headers=new_auth).json()
    assert len(cart["items"]) == 2                      # 3 distinct lines went in
    assert guest.get(CART).json()["items"] == []        # the guest cart is gone


def test_2B11_expiry_sweep_aborts_on_first_error(client, db_session, monkeypatch):
    """2B-11, Low. app/tasks.py:492-526 (no per-order try; the task has no
    autoretry).

    Scenario: two orders are due; a transient OperationalError (e.g. a
    deadlock, DB-01) on the first aborts the run and the second is not
    cancelled either.

    When fixed, invert the assertion: the sweep logs the failure, goes on,
    and cancels the other order.
    """
    shop = Shop(client, db_session)
    product = shop.product(stock=5)
    order_ids = [shop.order_of(shop.buyer(), product)["id"] for _ in range(2)]
    for order_id in order_ids:
        db_session.get(Order, order_id).created_at = utc_now() - timedelta(
            minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES + 1)
    db_session.commit()

    def failing_cancel(db, order):
        raise OperationalError("UPDATE products ...", {}, Exception("deadlock detected"))

    monkeypatch.setattr(tasks, "cancel_order_and_restock", failing_cancel)
    with TestingSessionLocal() as sweep, pytest.raises(OperationalError):
        tasks.expire_unpaid_orders(sweep)
    assert [order_state(shop, order_id)[0] for order_id in order_ids] == ["new", "new"]


def test_2C12_deletion_confirmation_claims_data_already_deleted(client, db_session, mailbox):
    """2C-12, Low. app/services/gdpr.py:143-145 sends
    send_gdpr_deletion_confirmation at request time; messages.py:373 says
    "your personal data has been deleted".

    Scenario: right after the erasure request the user is told the data has
    been deleted while the account row still holds the email, name and
    address (the purge runs after the grace period).

    When fixed, invert the assertion: the request-time mail says "scheduled"
    and the "has been deleted" mail is sent by the purge.
    """
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    response = client.post(f"{LEGAL}/data-request", headers=buyer.headers,
                           json={"request_type": "deletion", "password": BUYER_PW})
    assert response.status_code == 200
    assert any("your personal data has been deleted" in body for _to, _subj, body in mailbox.sent)
    db_session.expire_all()
    user = db_session.get(User, buyer.user_id)
    assert (user.email, user.full_name) == (buyer.email, "Buyer One")
    assert db_session.get(Address, buyer.address_id).street == "1 Main Street"


def test_2D02_OBS01_importing_the_celery_app_configures_no_logging():
    """OBS-01 (2D-02), Low. app/main.py:26 is the only setup_logging() call;
    app/tasks.py never connects celery.signals.setup_logging.

    Scenario: a worker process imports app.tasks; no JSON handler is
    installed, app.main is not loaded and the "app" logger level is unset.

    When fixed, invert the assertion: importing the worker module (or the
    signal) installs the application's handlers.
    """
    code = ("import logging, sys, app.tasks; "
            "print('app.main' in sys.modules, len(logging.getLogger().handlers), "
            "logging.getLogger('app').level)")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=dict(os.environ),
                         capture_output=True, text=True, check=True, timeout=120)
    assert out.stdout.split() == ["False", "0", "0"]


def test_2D03_deletion_mail_is_sent_inside_the_open_transaction(client, db_session, monkeypatch):
    """2D-03, Low. app/api/v1/legal.py:346-348 -> app/services/gdpr.py:126-141
    (send_gdpr_request_received, synchronous SMTP, runs inside the transaction
    _load_user opened; on PostgreSQL >30 s of SMTP kills it).

    Scenario: during the "request received" mail of an erasure request the
    request's AsyncSession is in a transaction.

    When fixed, invert the assertion: in_transaction() is False at send time
    (commit first, then mail).
    """
    sessions, states = [], []
    real_init = gdpr_mod.GDPRService.__init__

    def capturing_init(self, db, *args, **kwargs):
        sessions.append(db)
        real_init(self, db, *args, **kwargs)

    def received(*args, **kwargs):
        states.append(sessions[-1].in_transaction())
        return True

    monkeypatch.setattr(gdpr_mod.GDPRService, "__init__", capturing_init)
    monkeypatch.setattr("app.services.email.send_gdpr_request_received", received)
    monkeypatch.setattr("app.services.email.send_gdpr_deletion_confirmation", lambda *a, **k: True)
    buyer = Shop(client, db_session).buyer()
    response = client.post(f"{LEGAL}/data-request", headers=buyer.headers,
                           json={"request_type": "deletion", "password": BUYER_PW})
    assert response.status_code == 200
    assert states == [True]


def test_2D04_database_uri_is_not_url_encoded(monkeypatch):
    """2D-04, Low. app/core/config.py:208 (DATABASE_URI built by f-string
    interpolation of POSTGRES_PASSWORD).

    Scenario: POSTGRES_PASSWORD='p@ssw0rd-1234567890ab' yields a URL whose
    host is 'ssw0rd-1234567890ab@db'.

    When fixed, invert the assertion: host == 'db' and the password
    round-trips (URL.create(...)).
    """
    monkeypatch.delenv("DATABASE_URI", raising=False)
    built = Settings(_env_file=None, POSTGRES_SERVER="db",
                     POSTGRES_PASSWORD="p@ssw0rd-1234567890ab")
    url = make_url(built.DATABASE_URI.get_secret_value())
    assert url.host == "ssw0rd-1234567890ab@db"
    assert url.password != "p@ssw0rd-1234567890ab"


def test_2D07_order_email_celery_tasks_are_never_enqueued():
    """2D-07, Low. app/tasks.py:194-284 (send_order_confirmation /
    send_order_status_update with retries, dedup and routing) are dead: only
    process_stripe_event and check_payment_reconciliation are ever
    apply_async-ed (stripe_events.py:474, admin.py:939); order emails go out
    from BackgroundTasks with no retry.

    Scenario: scan app/ for .delay/.apply_async/.send_task receivers.

    When fixed, invert the assertion: the order email tasks are enqueued after
    commit, or deleted together with their config.
    """
    enqueued = set()
    for path in (REPO / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and node.attr in ("delay", "apply_async", "send_task")):
                target = node.value
                name = target.attr if isinstance(target, ast.Attribute) else getattr(
                    target, "id", None)
                if name:
                    enqueued.add(name)
    assert enqueued == {"process_stripe_event", "check_payment_reconciliation"}
    beat = {entry["task"] for entry in tasks.celery.conf.beat_schedule.values()}
    assert not beat & {"send_order_confirmation", "send_order_status_update"}


def test_BUGNEW1_redis_blip_on_cart_read_answers_400_cart_is_empty(client, db_session, monkeypatch):
    """BUG-NEW-1 (found by 3C, confirmed in 04_validation_phase3.md), Low.
    app/services/redis_async.py:213-222 (get_cart swallows RedisError and
    returns None); app/api/v1/orders.py:211-216 maps a falsy cart to 400.

    Scenario: a transient Redis error while checkout reads the cart makes it
    answer 400 "Cart is empty" with no Retry-After; the same request succeeds
    right after, because the cart was intact all along.

    When fixed, invert the assertion: the blip answers 503 with Retry-After.
    """
    from redis.exceptions import ConnectionError as RedisConnectionError

    import app.services.redis_async as redis_async

    reset_provider()
    shop = Shop(client, db_session)
    product = shop.product(stock=5)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product).status_code == 200

    real_load = redis_async.codec.load_cart

    def blip(*args, **kwargs):
        raise RedisConnectionError("simulated blip on GET cart")

    monkeypatch.setattr(redis_async.codec, "load_cart", blip)
    during = shop.place_order(buyer)
    assert during.status_code == 400
    assert "Cart is empty" in during.text
    assert "retry-after" not in during.headers

    monkeypatch.setattr(redis_async.codec, "load_cart", real_load)
    assert shop.place_order(buyer).status_code == 201
