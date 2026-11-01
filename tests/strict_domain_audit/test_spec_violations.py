"""Strict domain audit: reproductions of every confirmed SPEC-* violation.

Each xfail(strict=True) test asserts the SPEC-compliant behaviour: it fails
today and XPASSes (an error under strict_xfail) once the fix
is applied, which is the cue to drop the marker. Tests without the marker
are controls that pass before and after the fix (they keep the fix from
over-blocking).

  SPEC-CAT-02    parent/variant exclusivity (cart add/update, checkout)
  SPEC-CAT-03    product ETag covers variant state
  SPEC-CAT-04    order lines snapshot product name / variant SKU
  SPEC-CART-02   guest-cart merge quantity is bounded
  SPEC-OAT-021   cap on concurrent pending (unpaid) orders per user
  SPEC-LEDGER-02 dispute landing during a refund is not a silent success
  SPEC-ECOM-042  expiry does not cancel an order mid-3DS
  SPEC-AUTH-02   last active admin cannot self-erase
  SPEC-GDPR-01   erasure vs active fulfilment/dispute; late webhook PII
"""
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "integration"))

from payment_support import (
    PAY_3DS,
    Outbox,
    Shop,
    order_state,
    reset_provider,
)

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.core.security import create_access_token, get_password_hash
from app.main import app
from app.models.payment import WebhookEvent
from app.models.payment_ledger import PaymentRefund
from app.models.product import Product
from app.models.user import User
from app.schemas.common import INT32_MAX, Cart
from app.services.payment.stripe_provider import StripePaymentProvider
from tests.integration.test_admin import _auth_headers as _bearer
from tests.integration.test_admin import _login_as
from tests.integration.test_orders import (
    CART_PREFIX,
    ORDERS_PREFIX,
    _add_to_cart,
    _checkout_ready_buyer,
    _make_admin,
    _make_category,
    _make_product,
    _make_variant,
    _place_order,
)

ADMIN_PREFIX = "/api/v1/admin"
CATALOG_PREFIX = "/api/v1/products"
USERS_PREFIX = "/api/v1/users"

REJECTED = (409, 422)


# -- SPEC-CAT-02: a product with active variants cannot be bought bare --------------


def test_cart_add_rejects_bare_parent_when_product_has_active_variants(client, db_session):
    token, _ = _checkout_ready_buyer(client, db_session)
    product = _make_product(db_session, _make_category(db_session).id, stock=10)
    _make_variant(db_session, product.id)

    response = _add_to_cart(client, token, product.id, 2)  # no variant_id

    assert response.status_code in REJECTED


def test_cart_update_rejects_bare_parent_when_product_has_active_variants(client, db_session):
    token, _ = _checkout_ready_buyer(client, db_session)
    product = _make_product(db_session, _make_category(db_session).id, stock=10)
    assert _add_to_cart(client, token, product.id, 1).status_code == 200  # no variants yet
    _make_variant(db_session, product.id)

    response = client.put(
        f"{CART_PREFIX}/items/{product.id}", json={"quantity": 2}, headers=_bearer(token)
    )

    assert response.status_code in REJECTED


def test_create_order_rejects_bare_line_when_product_has_active_variants(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    product = _make_product(db_session, _make_category(db_session).id, stock=10)
    assert _add_to_cart(client, token, product.id, 2).status_code == 200  # no variants yet
    _make_variant(db_session, product.id)  # the product gains a variant afterwards

    response = _place_order(client, token, address_id)

    assert response.status_code in REJECTED
    db_session.expire_all()
    assert db_session.get(Product, product.id).stock_quantity == 10


# -- SPEC-CAT-03: the product ETag covers variant state ----------------------------


@pytest.mark.xfail(strict=True, reason="SPEC-CAT-03: GET /products/{id} ETag does not change when a variant is mutated")
@pytest.mark.parametrize(
    "change",
    [
        {"price_override": "99.00"},
        {"stock_quantity": 1},
        {"sku": "CHANGED-SKU"},
        {"is_active": False},
    ],
    ids=["price", "stock", "sku", "is_active"],
)
def test_product_etag_changes_when_a_variant_changes(client, db_session, change):
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")
    product = _make_product(db_session, _make_category(db_session).id)
    variant = _make_variant(db_session, product.id, price_override="12.00", stock=5)
    url = f"{CATALOG_PREFIX}/{product.id}"
    before = client.get(url).headers["etag"]

    put = client.put(
        f"{ADMIN_PREFIX}/products/{product.id}/variants/{variant.id}",
        json=change,
        headers=_bearer(admin_token),
    )
    assert put.status_code == 200

    revalidated = client.get(url, headers={"If-None-Match": before})
    assert revalidated.status_code == 200  # not 304: the resource changed
    assert revalidated.headers["etag"] != before


# -- SPEC-CAT-04: historical orders keep what was bought ---------------------------


def _order_with_variant_line(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    product = _make_product(db_session, _make_category(db_session).id)
    variant = _make_variant(db_session, product.id, sku="WIDGET-M-RED", price_override="12.00")
    assert _add_to_cart(client, token, product.id, 1, variant_id=variant.id).status_code == 200
    created = _place_order(client, token, address_id)
    assert created.status_code == 201
    line = created.json()["items"][0]
    assert (line["product_name"], line["variant_sku"]) == ("Widget", "WIDGET-M-RED")
    return token, created.json()["id"], product, variant


@pytest.mark.xfail(strict=True, reason="SPEC-CAT-04: renaming a product changes product_name of historical orders")
def test_order_keeps_the_product_name_it_was_placed_with(client, db_session):
    token, order_id, product, _ = _order_with_variant_line(client, db_session)
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    renamed = client.put(
        f"{ADMIN_PREFIX}/products/{product.id}", json={"name": "RENAMED PRODUCT"}, headers=_bearer(admin_token)
    )
    assert renamed.status_code == 200

    line = client.get(f"{ORDERS_PREFIX}/{order_id}", headers=_bearer(token)).json()["items"][0]
    assert line["product_name"] == "Widget"


@pytest.mark.xfail(strict=True, reason="SPEC-CAT-04: changing a variant SKU changes variant_sku of historical orders")
def test_order_keeps_the_variant_sku_it_was_placed_with(client, db_session):
    token, order_id, product, variant = _order_with_variant_line(client, db_session)
    admin = _make_admin(db_session)
    admin_token = _login_as(client, admin.email, "AdminPass1")

    changed = client.put(
        f"{ADMIN_PREFIX}/products/{product.id}/variants/{variant.id}",
        json={"sku": "CHANGED-SKU"},
        headers=_bearer(admin_token),
    )
    assert changed.status_code == 200

    line = client.get(f"{ORDERS_PREFIX}/{order_id}", headers=_bearer(token)).json()["items"][0]
    assert line["variant_sku"] == "WIDGET-M-RED"


# -- SPEC-CART-02: a merged quantity is clamped (or refused), never a 500 ----------


def _cart_with(quantity):
    cart = Cart(expires_at=utc_now())
    cart.add_item(1, quantity, price=None, name="Widget", image="")
    return cart


def test_cart_merge_never_exceeds_the_quantity_bound():
    mine = _cart_with(2_000_000_000)
    theirs = _cart_with(2_000_000_000)

    mine.merge(theirs, max_lines=100)

    assert all(item.quantity <= INT32_MAX for item in mine.items.values())


def test_checkout_after_an_overflowing_guest_merge_is_not_a_server_error(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    product = _make_product(db_session, _make_category(db_session).id, stock=2_000_000_000)
    assert _add_to_cart(client, token, product.id, 2_000_000_000).status_code == 200
    guest = TestClient(app, base_url="https://testserver")
    assert guest.post(
        f"{CART_PREFIX}/items", json={"product_id": product.id, "quantity": 2_000_000_000}
    ).status_code == 200
    login = guest.post(
        f"{USERS_PREFIX}/login", data={"username": "buyer@example.com", "password": "Str0ngPass1"}
    )
    assert login.status_code == 200

    cart = client.get(CART_PREFIX, headers=_bearer(token)).json()
    assert all(item["quantity"] <= INT32_MAX for item in cart["items"])
    order = _place_order(client, token, address_id)
    assert order.status_code in (201, 409)  # a clean answer, not "integer out of range"


# -- SPEC-OAT-021: concurrent pending orders per user are capped -------------------


@pytest.mark.xfail(strict=True, reason="SPEC-OAT-021: no cap on concurrent pending (stock-reserving) orders per user")
def test_pending_orders_per_user_are_capped(client, db_session):
    token, address_id = _checkout_ready_buyer(client, db_session)
    product = _make_product(db_session, _make_category(db_session).id, stock=1000)

    statuses = []
    for _ in range(40):
        assert _add_to_cart(client, token, product.id, 1).status_code == 200
        statuses.append(_place_order(client, token, address_id).status_code)
        if statuses[-1] != 201:
            break

    assert statuses[-1] in (409, 429), f"{statuses.count(201)} unpaid orders accepted, none refused"
    # A refused order reserved nothing.
    db_session.expire_all()
    assert db_session.get(Product, product.id).stock_quantity == 1000 - statuses.count(201)



# ======================= part B: ledger / auth / GDPR =======================

LEGAL = "/api/v1/legal"
PASSWORD = "Str0ngPass1"
DELETION = {"request_type": "deletion", "password": PASSWORD}


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def outbox(monkeypatch):
    return Outbox(monkeypatch)


@pytest.fixture
def shop(client, db_session, monkeypatch, tasks_db):
    for name in ("send_gdpr_request_received", "send_gdpr_deletion_confirmation"):
        monkeypatch.setattr(f"app.services.email.{name}", lambda *a, **k: True)
    return Shop(client, db_session)


def _dispute(payment, dispute_id="du_b_1", status="needs_response", **extra):
    return {"id": dispute_id, "object": "dispute", "amount": payment.amount, "currency": "usd",
            "payment_intent": payment.payment_intent_id, "status": status,
            "reason": "fraudulent", **extra}


def _paid_order(shop, price="19.99"):
    product = shop.product(price=price, stock=4)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"]).status_code == 200
    return buyer, order, product, shop.payments(order["id"])[0]


def _request_deletion(shop, headers):
    return shop.client.post(f"{LEGAL}/data-request", json=DELETION, headers=headers)


def _is_active(shop, email):
    shop.db.expire_all()
    return shop.db.query(User).filter_by(email=email).one().is_active


# -- SPEC-LEDGER-02 ----------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=(
    "SPEC-LEDGER-02: a dispute that lands during the provider refund call is not "
    "re-checked; the refund is answered 200 with no manual review flag"))
def test_dispute_landing_during_the_refund_call_is_recorded_and_flagged_for_review(shop, monkeypatch):
    _, order, _, payment = _paid_order(shop)
    admin = shop.admin()
    real_create_refund = StripePaymentProvider.create_refund

    def refund_while_dispute_lands(self, *args, **kwargs):
        shop.webhook("charge.dispute.created", _dispute(payment))
        return real_create_refund(self, *args, **kwargs)

    monkeypatch.setattr(StripePaymentProvider, "create_refund", refund_while_dispute_lands)

    response = shop.refund(admin, order["id"], amount="5.00")

    # Money has moved at the provider, so it is recorded with 200 status,
    # but requires_manual_review must be flagged True for investigation.
    assert response.status_code == 200, response.text
    shop.db.expire_all()
    rows = shop.db.query(PaymentRefund).all()
    assert [(r.amount, r.requires_manual_review) for r in rows] == [(500, True)]


# -- SPEC-ECOM-042 -----------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=(
    "SPEC-ECOM-042: expire_unpaid_orders cancels an order whose 3DS attempt "
    "(requires_action) is still live"))
def test_expiry_leaves_an_order_with_a_live_3ds_attempt_alone(shop, outbox):
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    assert shop.payments(order["id"])[0].status == "requires_action"

    cancelled = shop.expire_unpaid(order["id"])

    assert cancelled == 0
    assert order_state(shop, order["id"]) == ("new", "pending")
    assert shop.stock(product) == 3  # still reserved for the customer in 3DS


def test_expiry_still_cancels_when_the_3ds_attempt_is_stale(shop, outbox):
    # Guard against a permanent lock: an attempt abandoned in 3DS only defers.
    product = shop.product(price="19.99", stock=4)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product)
    assert shop.pay(buyer, order["id"], token=PAY_3DS).status_code == 200
    attempt = shop.payments(order["id"])[0]
    stale = utc_now() - timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES + 1)
    attempt.created_at = attempt.updated_at = stale
    shop.db.commit()

    cancelled = shop.expire_unpaid(order["id"])

    assert cancelled == 1
    assert order_state(shop, order["id"]) == ("cancelled", "pending")
    assert shop.stock(product) == 4


# -- SPEC-AUTH-02 ------------------------------------------------------------


def _admin(db, email):
    user = User(email=email, hashed_password=get_password_hash(PASSWORD), role="admin",
                is_active=True, is_email_verified=True, gdpr_consent=True,
                privacy_policy_accepted=True)
    db.add(user)
    db.commit()
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


def test_the_last_active_admin_cannot_request_erasure(shop):
    headers = _admin(shop.db, "only-admin@example.com")

    response = _request_deletion(shop, headers)

    assert response.status_code == 409, response.text
    assert _is_active(shop, "only-admin@example.com") is True


def test_an_admin_may_request_erasure_while_another_admin_is_active(shop):
    _admin(shop.db, "other-admin@example.com")
    headers = _admin(shop.db, "leaving-admin@example.com")

    response = _request_deletion(shop, headers)

    assert response.status_code == 200, response.text
    assert _is_active(shop, "leaving-admin@example.com") is False


# -- SPEC-GDPR-01 ------------------------------------------------------------


@pytest.mark.xfail(strict=True, reason=(
    "SPEC-GDPR-01(a): erasure must be accepted while an order is in fulfilment, "
    "and the order must retain a snapshot of its shipping address after purge"))
def test_erasure_is_accepted_and_preserves_fulfilment_address_snapshot(shop):
    buyer, order, _, _ = _paid_order(shop)
    admin = shop.admin()
    assert shop.set_status(admin, order["id"], "processing").status_code == 200
    assert shop.set_status(admin, order["id"], "sent").status_code == 200

    response = _request_deletion(shop, buyer.headers)

    # Erasure request is accepted; user account is deactivated
    assert response.status_code == 200, response.text
    assert _is_active(shop, buyer.email) is False

    # Simulate purge after grace period
    user = shop.db.get(User, buyer.user_id)
    user.data_deletion_date = utc_now() - timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 1)
    shop.db.commit()
    tasks.purge_expired_deletion_requests(shop.db)

    # The order still in fulfilment must preserve its shipping address snapshot
    order_row = shop.order(order["id"])
    assert getattr(order_row, "ship_to_street", None) or order_row.shipping_address.street != "REDACTED"


@pytest.mark.xfail(strict=True, reason=(
    "SPEC-GDPR-01(a): erasure must be accepted while a dispute is open, "
    "and the order must retain a snapshot of its shipping address after purge"))
def test_erasure_is_accepted_and_preserves_disputed_order_address_snapshot(shop):
    buyer, order, _, payment = _paid_order(shop)
    admin = shop.admin()
    for status in ("processing", "sent", "delivered"):
        assert shop.set_status(admin, order["id"], status).status_code == 200
    shop.webhook("charge.dispute.created", _dispute(payment))

    response = _request_deletion(shop, buyer.headers)

    # Erasure request is accepted; user account is deactivated
    assert response.status_code == 200, response.text
    assert _is_active(shop, buyer.email) is False

    # Simulate purge after grace period
    user = shop.db.get(User, buyer.user_id)
    user.data_deletion_date = utc_now() - timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS + 1)
    shop.db.commit()
    tasks.purge_expired_deletion_requests(shop.db)

    # Order under open dispute must retain its shipping address snapshot
    order_row = shop.order(order["id"])
    assert getattr(order_row, "ship_to_street", None) or order_row.shipping_address.street != "REDACTED"


def test_erasure_is_accepted_once_orders_are_delivered_and_undisputed(shop):
    buyer, order, _, _ = _paid_order(shop)
    admin = shop.admin()
    for status in ("processing", "sent", "delivered"):
        assert shop.set_status(admin, order["id"], status).status_code == 200

    response = _request_deletion(shop, buyer.headers)

    assert response.status_code == 200, response.text
    assert _is_active(shop, buyer.email) is False


@pytest.mark.xfail(strict=True, reason=(
    "SPEC-GDPR-01(b): a Stripe event stored after the purge keeps the erased "
    "customer's raw personal data"))
def test_event_stored_after_the_purge_is_redacted(shop):
    buyer, order, _, payment = _paid_order(shop)
    user = shop.db.get(User, buyer.user_id)
    user.is_active = False
    user.data_deletion_requested = True
    user.data_deletion_date = utc_now() - timedelta(days=3)
    shop.db.commit()
    assert tasks.purge_expired_deletion_requests(shop.db) == 1

    late_email = "late-dispute-pii@example.com"
    shop.webhook(
        "charge.dispute.created",
        _dispute(payment, metadata={"order_id": str(order["id"])},
                 evidence={"customer_email_address": late_email,
                           "billing_address": "1 Main Street"}),
        event_id="evt_late_dispute",
    )

    shop.db.expire_all()
    stored = shop.db.query(WebhookEvent).filter_by(event_id="evt_late_dispute").one()
    assert late_email not in stored.payload
    assert json.loads(stored.payload)["redacted"] is True
