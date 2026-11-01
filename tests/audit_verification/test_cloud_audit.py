"""Reproductions of confirmed audit findings.

Every test here asserts the correct behaviour and carries an xfail marker.
When a finding is fixed in subsequent commits, the test turns XPASS, signalling
that the xfail marker should be dropped.

The PostgreSQL-only reproductions (DB-01 deadlock, GDPR-02 idle-in-transaction
timeout) live in test_postgres_cloud_audit.py next to this file, which
tests/conftest.py marks db_sensitive by its name.
"""

import dataclasses
import json
import os
import subprocess
import sys
from datetime import timedelta
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

from conftest import TestingSessionLocal  # noqa: E402
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


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: PAY-01 late 3DS charge on expired order is unrefundable and unreported")
def test_PAY01_late_3ds_charge_on_expired_order_is_refundable_or_reported(
    client, db_session, tasks_db
):
    """PAY-01 (2A-01), High. Correct behaviour: expiry must skip an order with an
    in-flight 3DS attempt, or the late charge must be refundable and surfaced
    by reconciliation.
    """
    shop, product, buyer, order = _placed_order(client, db_session)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    assert shop.expire_unpaid(order["id"]) == 0          # in-flight attempt must not be expired
    assert shop.stock(product) == 0                      # stock remains reserved for 3DS attempt
    late = shop.intent_event("payment_intent.succeeded", shop.payments(order["id"])[0])
    assert late.status_code == 200
    assert shop.refund(shop.admin(), order["id"]).status_code == 200


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: PAY-01 customer cancel accepts cancel during in-flight 3DS attempt")
def test_PAY01_customer_cancel_refuses_in_flight_3ds_attempt(client, db_session):
    """PAY-01 (2B-03), High. Correct behaviour: customer cancel must answer 409
    while an attempt is in flight.
    """
    shop, _product, buyer, order = _placed_order(client, db_session)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    assert shop.cancel(buyer, order["id"]).status_code == 409   # must be refused mid-3DS


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: PAY-01 second checkout session allowed after stale window double charges")
def test_PAY01_second_checkout_session_refused_or_first_session_expired(client, db_session):
    """PAY-01 (2A-04), High. Correct behaviour: the second checkout-session must
    answer 409 while the first session can still be paid, or the first session must
    be expired once the second succeeds.
    """
    shop, _product, buyer, order = _placed_order(client, db_session)
    assert shop.checkout_session(buyer, order["id"]).status_code == 200
    first = shop.payments(order["id"])[0]
    first.created_at = utc_now() - timedelta(
        minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES + 1)
    db_session.commit()

    second_session = shop.checkout_session(buyer, order["id"])
    assert second_session.status_code == 409, second_session.text


# == Medium ====================================================================


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: PAY-02 checkout session without webhook is skipped by reconciliation")
def test_PAY02_checkout_session_without_webhook_is_reconciled(client, db_session):
    """PAY-02 (2A-02), Medium. Correct behaviour: reconciliation checks
    sessions with pending intent IDs against the provider.
    """
    shop, _product, buyer, order = _placed_order(client, db_session)
    assert shop.checkout_session(buyer, order["id"]).status_code == 200
    assert shop.payments(order["id"])[0].payment_intent_id.startswith("pending-cs-")
    result = reconcile_payments(db_session, StripePaymentProvider(),
                                now=utc_now() + timedelta(hours=2))
    assert (result.checked, result.skipped) == (1, 0)


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: PAY-03 stale pending refund event regresses refunded order to paid")
def test_PAY03_stale_pending_refund_event_does_not_regress_refunded_order_to_paid(client, db_session):
    """PAY-03 (2A-03), Medium. Correct behaviour: payment_status stays 'refunded'
    and setting status to processing answers 409.
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
    assert order_state(shop, order["id"])[1] == "refunded"
    assert shop.set_status(admin, order["id"], "processing").status_code == 409


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: GDPR-01 webhook event for erased user retains PII")
def test_GDPR01_webhook_event_for_erased_user_is_redacted_on_processing(db_session):
    """GDPR-01 (2C-01 + 2D-06 last_error), Medium. Correct behaviour: events
    about a customer whose erasure was requested get redacted when processed.
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
    stripe_events.process_stored_event(db_session, "evt_after")
    db_session.expire_all()
    row = db_session.query(WebhookEvent).filter_by(event_id="evt_after").one()
    assert "jane@example.com" not in row.payload


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: GDPR-02 purge hashes bcrypt inside open transaction")
def test_GDPR02_purge_does_not_hash_bcrypt_inside_open_transaction(monkeypatch):
    """GDPR-02 (2D-01 + 2D-C18), Medium. Correct behaviour: no bcrypt hash runs
    while holding an open transaction.
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
    assert all(not in_tx for in_tx, _ in seen)


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: GDPR-03 patch me changes marketing consent without history")
def test_GDPR03_patch_me_changes_marketing_consent_records_history(client, db_session):
    """GDPR-03 (2C-07), Medium. Correct behaviour: marketing_consent_date is set
    and consent_history records the update.
    """
    tokens = _register_and_login(client, "c@example.com", marketing_consent=False)
    response = client.patch(f"{USERS}/me", json={"marketing_consent": True},
                            headers={"Authorization": f"Bearer {tokens['access_token']}"})
    assert response.status_code == 200
    db_session.expire_all()
    user = db_session.query(User).filter_by(email="c@example.com").one()
    assert user.marketing_consent is True
    assert user.marketing_consent_date is not None
    assert [e for e in user.consent_history if e["type"] == "marketing"][-1]["granted"] is True


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: GDPR-04 erasure purge redacts shipping address needed for paid unshipped order")
def test_GDPR04_order_retains_shipping_address_snapshot_after_erasure_purge(client, db_session):
    """GDPR-04 (2C-10), Medium. Correct behaviour: a paid unshipped order retains
    a usable shipping address snapshot after the customer's account is purged.
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
    assert getattr(stored, "ship_to_street", None) or db_session.get(Address, stored.shipping_address_id).street != "REDACTED"


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: GDPR-05 data export contains no personal data")
def test_GDPR05_data_export_contains_personal_data(client, db_session, mailbox):
    """GDPR-05 (2C-11), Medium. Correct behaviour: export contains personal data
    and data_export_requests records the request.
    """
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    response = client.post(f"{LEGAL}/data-request", json={"request_type": "export"},
                            headers=buyer.headers)
    assert response.status_code == 200
    [(_to, _subject, body)] = [
        m for m in mailbox.sent if "here is your personal data export" in m[2]]
    for personal in ("Buyer One", "1 Main Street", "Metropolis", "+15551234567"):
        assert personal in body
    db_session.expire_all()
    assert len(db_session.get(User, buyer.user_id).data_export_requests or []) >= 1


def test_AUTH01_login_in_flight_during_password_reset_is_revoked(client, db_session, monkeypatch):
    """AUTH-01 (2C-02), Medium. Correct behaviour: tokens minted from an in-flight
    login during password reset are rejected with 401.
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
        response = client.post(
            f"{USERS}/login",
            data={"username": "r@example.com", "password": PW},
        )
    assert response.status_code == 401


def test_AUTH01_refresh_rotation_in_flight_during_reset_is_revoked(client, db_session, monkeypatch):
    """AUTH-01 (2C-03), Medium. Correct behaviour: refresh token rotated during
    password reset is revoked.
    """
    import threading

    tokens = _register_and_login(client, "s@example.com")
    user = db_session.query(User).filter_by(email="s@example.com").one()
    reset_token = sec.generate_password_reset_token(user.email, user.hashed_password)
    real = sec.is_token_superseded

    async def check_then_reset(user_id, iat):
        result = await real(user_id, iat)
        def _call_reset():
            with TestClient(app) as c:
                res = c.post(
                    f"{USERS}/password/reset/confirm",
                    json={"token": reset_token, "new_password": "N3w-Password-1!"},
                )
                assert res.status_code == 200, res.text
        t = threading.Thread(target=_call_reset)
        t.start()
        t.join()
        return result

    with monkeypatch.context() as m:
        m.setattr(sec, "is_token_superseded", check_then_reset)
        rotated = client.post(f"{USERS}/refresh", json={"refresh_token": tokens["refresh_token"]})
    if rotated.status_code == 200:
        again = client.post(f"{USERS}/refresh", json={"refresh_token": rotated.json()["refresh_token"]})
        assert again.status_code == 401
    else:
        assert rotated.status_code == 401


def test_AUTH02_deletion_reauth_is_rate_limited_and_locks_out(client, db_session, monkeypatch):
    """AUTH-02 (2C-04), Medium. Correct behaviour: deletion reauth is in
    _ROUTE_LIMITS and repeated failures trigger lockout.
    """
    for name in ("send_gdpr_request_received", "send_gdpr_deletion_confirmation"):
        monkeypatch.setattr(f"app.services.email.{name}", lambda *a, **k: True)
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    assert ("POST", "/legal/data-request") in _ROUTE_LIMITS
    token = _register_and_login(client, "u@example.com")["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    for i in range(30):
        guess = client.post(f"{LEGAL}/data-request", headers=headers,
                            json={"request_type": "deletion", "password": f"guess-{i}"})
        assert guess.status_code in (401, 429, 423)
    hit = client.post(f"{LEGAL}/data-request", headers=headers,
                      json={"request_type": "deletion", "password": PW})
    assert hit.status_code in (423, 429)


def test_REG01_registration_without_full_name_sends_verification_mail(
    client, db_session, monkeypatch, mailbox
):
    """REG-01 (2C-05), Medium. Correct behaviour: registration without full_name
    sends verification mail without raising AttributeError.
    """
    errors = []
    real_send = email_messages.send_welcome_email

    def recording_send(*args, **kwargs):
        try:
            return real_send(*args, **kwargs)
        except Exception as exc:
            errors.append(exc)
            return False

    monkeypatch.setattr(users_mod, "send_welcome_email", recording_send)
    response = client.post(f"{USERS}/register", json={
        "email": "noname@example.com", "password": PW, "gdpr_consent": True,
        "privacy_policy_accepted": True})
    assert response.status_code == 202
    assert len(errors) == 0
    assert len(mailbox.sent) == 1
    db_session.expire_all()


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: DATA-01 address edit rewrites delivered order")
def test_DATA01_address_edit_preserves_id_and_historical_order_snapshot(client, db_session):
    """DATA-01 (2C-06), Medium. Correct behaviour: editing an address preserves
    its ID, while a previously delivered order retains its original address.
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
    assert edited.json()["id"] == address["id"]
    shown = client.get(f"/api/v1/orders/{order.id}", headers=headers).json()
    assert shown["shipping_address"]["street"] == "Main 1 st"


# == Low =======================================================================


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: 2A-05 pending rows consume the reconciliation cap")
def test_2A05_pending_rows_do_not_consume_reconciliation_cap(client, db_session, monkeypatch):
    """2A-05, Low. Correct behaviour: reconciliation cap is not consumed by
    abandoned pending rows.
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
    assert result.checked == 1


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: 2A-06 pay answers paid when success was not applied")
def test_2A06_pay_does_not_answer_paid_when_success_not_applied(client, db_session, monkeypatch):
    """2A-06, Low. Correct behaviour: /pay answers 409 or non-paid status when
    payment success was not applied.
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
    assert paid.status_code in (400, 409) or paid.json()["status"] != "paid"


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: 2A-07 orphaned processing attempt returns 409 forever")
def test_2A07_orphaned_processing_attempt_can_be_retried(client, db_session, monkeypatch):
    """2A-07, Low. Correct behaviour: a stale processing attempt can be retried."""
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
    assert again.status_code != 409


def _admin_headers(client, db_session):
    admin = _make_admin(db_session)
    return _auth_headers(_login_as(client, admin.email, "AdminPass1"))


def _char(name, value):
    return {"name": name, "value": value, "unit": None, "filterable": True}


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: 2B-04 merge patch replaces nested characteristics and rejects null")
def test_2B04_merge_patch_preserves_sibling_characteristics_and_removes_nulls(client, db_session):
    """2B-04, Low. Correct behaviour: merge patch preserves sibling keys
    and accepts null to remove a member.
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
    assert "size" in patched.json()["characteristics"]
    nulled = client.patch(f"{ADMIN_PREFIX}/products/{product['id']}",
                          json={"characteristics": {"color": None}},
                          headers={**headers, **MERGE_PATCH})
    assert nulled.status_code == 200


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: 2B-04 merge patch stores null in variant attributes")
def test_2B04_merge_patch_removes_null_in_variant_attributes(client, db_session):
    """2B-04, Low. Correct behaviour: merge patch removes key on null instead of storing it."""
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
    assert patched.json()["attributes"] == {"color": "red"}


def test_2B07_order_total_is_bounded_by_numeric_column(client, db_session):
    """2B-07, Low. Correct behaviour: order total exceeding column bounds is
    refused with a 4xx before committing.
    """
    shop = Shop(client, db_session)
    product = shop.product(price="1000.00", stock=200_000)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, product, quantity=100_001).status_code == 200
    placed = shop.place_order(buyer)
    assert 400 <= placed.status_code < 500, placed.text


def test_2B09_guest_cart_merge_reports_or_keeps_unmerged_lines(client, db_session, monkeypatch):
    """2B-09, Low. Correct behaviour: unmerged lines are kept or reported."""
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
    assert "left_out" in merged_login.text or len(guest.get(CART).json()["items"]) > 0


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: 2B-11 expiry sweep aborts on first error")
def test_2B11_expiry_sweep_continues_past_single_order_error(client, db_session, monkeypatch):
    """2B-11, Low. Correct behaviour: the sweep logs the failure, continues,
    and cancels remaining eligible orders.
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
    with TestingSessionLocal() as sweep:
        tasks.expire_unpaid_orders(sweep)
    assert "cancelled" in [order_state(shop, order_id)[0] for order_id in order_ids]


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: 2C-12 deletion confirmation claims data already deleted at request time")
def test_2C12_deletion_request_acknowledges_scheduled_deletion(client, db_session, mailbox):
    """2C-12, Low. Correct behaviour: request-time email states deletion is scheduled,
    not already completed.
    """
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    response = client.post(f"{LEGAL}/data-request", headers=buyer.headers,
                           json={"request_type": "deletion", "password": BUYER_PW})
    assert response.status_code == 200
    assert not any("your personal data has been deleted" in body for _to, _subj, body in mailbox.sent)
    assert any("scheduled" in body.lower() for _to, _subj, body in mailbox.sent)


def test_2D02_OBS01_importing_celery_app_configures_logging():
    """OBS-01 (2D-02), Low. Correct behaviour: celery setup_logging signal configures
    application JSON logging.
    """
    code = (
        "import logging, sys, app.tasks; "
        "from celery.signals import setup_logging; "
        "setup_logging.send(sender=None); "
        "root = logging.getLogger(); "
        "has_json = any(h.__class__.__name__ == 'JSONFormatter' or getattr(h, 'formatter', None).__class__.__name__ == 'JSONFormatter' for h in root.handlers); "
        "assert has_json, 'expected json formatter after setup_logging signal'"
    )
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, env=dict(os.environ),
                         capture_output=True, text=True, check=True, timeout=120)
    assert out.returncode == 0


@pytest.mark.xfail(strict=True, reason="BUG DISCOVERED: 2D-03 deletion mail is sent inside open transaction")
def test_2D03_deletion_mail_is_sent_outside_transaction(client, db_session, monkeypatch):
    """2D-03, Low. Correct behaviour: in_transaction() is False at email send time."""
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
    assert states == [False]


def test_2D04_database_uri_encodes_credentials_properly(monkeypatch):
    """2D-04, Low. Correct behaviour: host is parsed as 'db' and password round-trips."""
    monkeypatch.delenv("DATABASE_URI", raising=False)
    built = Settings(_env_file=None, POSTGRES_SERVER="db",
                     POSTGRES_PASSWORD="p@ssw0rd-1234567890ab")
    url = make_url(built.DATABASE_URI.get_secret_value())
    assert url.host == "db"
    assert url.password == "p@ssw0rd-1234567890ab"


def test_2D07_dead_order_email_celery_tasks_are_removed():
    """2D-07, Low. Correct behaviour: dead order email Celery tasks are removed."""
    assert not hasattr(tasks, "send_order_confirmation")
    assert not hasattr(tasks, "send_order_status_update")


def test_BUGNEW1_redis_blip_on_cart_read_answers_503_with_retry_after(client, db_session, monkeypatch):
    """BUG-NEW-1, Low. Correct behaviour: transient Redis read errors answer 503
    with Retry-After header.
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
    assert during.status_code == 503
    assert "retry-after" in during.headers

    monkeypatch.setattr(redis_async.codec, "load_cart", real_load)
    assert shop.place_order(buyer).status_code == 201
