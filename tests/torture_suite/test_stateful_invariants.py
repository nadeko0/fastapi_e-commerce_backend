# Stateful property-based torture tests (phase 2a).
#
# Two Hypothesis RuleBasedStateMachines drive the real HTTP API (TestClient on
# app.main.app, the in-process mock payment provider, the Celery task bodies
# run inline) and compare it with a pure-Python model after every step, plus
# @given properties that go beyond tests/integration/test_payment_state_machine.py
# and test_order_invariants.py:
#
# Model 1 - CartWorld: a guest (cookie jar) and an account cart over five cart
# lines (two plain products, a product with a plain line and two variants),
# with admin stock/price/variant/product changes in between, merge at login,
# validate and checkout. Every response is predicted exactly (status code and
# cart contents in order, Decimal totals).
#
# Model 2 - PaymentWorld: orders of several lines for two buyers; /pay with
# succeeding, declining, 3-D Secure, pending-refund and failing-refund cards;
# Checkout Sessions; idempotent replays; admin refunds, dashboard refunds,
# asynchronous refund settlement, disputes; customer/admin cancellation,
# admin status changes, unpaid-order expiry and admin stock changes. Stripe's
# side is the mock provider; every Stripe event goes into an outbox and is
# delivered later, in any order, and redelivered (same event id). The
# invariants (stock conservation, ledger, transition graph, problem+json, no
# 500) are checked after every step; a "drain" rule delivers everything in a
# random permutation and then checks that our database converged to what the
# provider says.
#
# See .cloud_torture/02a_stateful_hypothesis.md for the write-up and
# .cloud_torture/03b_validation_stateful_concurrency.md for the validation.
#
# Discovered backend defects, each with a deterministic test_bug_* reproducer
# at the end of this module marked xfail(strict=True) (an XPASS - the fix -
# fails the run until the marker is dropped):
#   bug 1  a stale refund event rewinds a settled refund (refunded -> paid)
#   bug 2  /refund answers 500 after an unreported refund failure
#   bug 3  restock past Int32Max: 500 on cancel / expiry sweep (PostgreSQL),
#          stock above the API's own maximum (SQLite)
# Known-bug allowances: the state machines and @given properties find these
# at random, so they must not be strict-xfailed; instead they tolerate (or
# step around) exactly the defect's signature - see KNOWN_BUG_ALLOWANCES -
# and keep every other invariant strict. TORTURE_STRICT=1 switches the
# allowances off and re-reveals the defects.
#
# Budgets: the defaults keep the module within a few minutes per back end
# under HYPOTHESIS_PROFILE=ci; TORTURE_FULL=1 restores the Phase 2 campaign
# budgets (and writes the per-run counters to .cloud_torture/scratch_2a/).
import json
import os
import random
import uuid
from collections import Counter
from datetime import timedelta
from decimal import Decimal

import pytest
import torture_support  # noqa: F401  (puts tests/integration on sys.path)
from conftest import IS_POSTGRES, scaled_examples
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    Bundle,
    RuleBasedStateMachine,
    initialize,
    invariant,
    multiple,
    precondition,
    rule,
    run_state_machine_as_test,
)
from payment_support import (
    PAY_3DS,
    PAY_DECLINED,
    PAY_OK,
    Shop,
    cents,
    new_key,
    reset_provider,
)

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.main import app
from app.models.category import Category
from app.models.order import Order
from app.models.payment import Payment, WebhookEvent
from app.models.payment_ledger import DISPUTE_CLOSED_STATUSES
from app.models.product import Product, ProductVariant
from app.services.orders import ORDER_STATUS_TRANSITIONS
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import CheckoutSessionStatus, PaymentIntentStatus

pytestmark = pytest.mark.db_sensitive

# TORTURE_STRICT=1: no known-bug allowance (see the header).
KNOWN_BUG_ALLOWANCES = os.environ.get("TORTURE_STRICT") != "1"
# TORTURE_FULL=1: the Phase 2 campaign budgets.
TORTURE_FULL = os.environ.get("TORTURE_FULL") == "1"
# Examples per test (scaled_examples doubles them under the ci/explore
# profiles): campaign budgets with TORTURE_FULL=1, a CI-sized share otherwise.
CART_WORLD_EXAMPLES = 100 if TORTURE_FULL else 30
PAYMENT_WORLD_EXAMPLES = 150 if TORTURE_FULL else 40
GIVEN_EXAMPLES = 300 if TORTURE_FULL else 50

CART = "/api/v1/cart"
ORDERS = "/api/v1/orders"
ADMIN = "/api/v1/admin"
LOGIN = "/api/v1/users/login"
PASSWORD = "Str0ngPass1"  # Shop.buyer()'s password
GUEST_COOKIE = "__Host-guest_cart"
INT32_MAX = 2_147_483_647
PROBLEM_JSON = "application/problem+json"

PAY_PENDING_REFUND = "pm_card_pendingRefund"
PAY_REFUND_FAIL = "pm_card_refundFail"
PAY_3DS_DECLINE = "pm_card_threeDSecureRequiredChargeDeclined"
SUCCEEDING = {PAY_OK, PAY_PENDING_REFUND, PAY_REFUND_FAIL}
AUTHENTICATING = {PAY_3DS, PAY_3DS_DECLINE}

STATS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".cloud_torture", "scratch_2a",
)

# Answers seen, by "<route> <code>", and transitions observed, per test.
SEEN = Counter()
TRANSITIONS = Counter()

_HYPOTHESIS = dict(
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow,
                           HealthCheck.filter_too_much, HealthCheck.data_too_large],
)

# A flag that is usually False: Hypothesis favours the simplest value of a
# strategy (0, False) when it generates, so a rare action is gated on True.
def rarely(one_in):
    return st.sampled_from([False] * (one_in - 1) + [True])


PRICES = st.decimals(min_value=Decimal("0.00"), max_value=Decimal("999.99"), places=2,
                     allow_nan=False, allow_infinity=False)
# Model 2 charges orders: the provider refuses a 0.00 charge, so its prices are positive.
CHARGEABLE = st.decimals(min_value=Decimal("0.50"), max_value=Decimal("250.00"), places=2,
                         allow_nan=False, allow_infinity=False)


def _dump_stats(name):
    if not TORTURE_FULL:
        return  # only campaign runs write to the repository
    os.makedirs(STATS_DIR, exist_ok=True)
    backend = "postgres" if IS_POSTGRES else "sqlite"
    with open(os.path.join(STATS_DIR, f"stats_{name}_{backend}.json"), "w") as handle:
        json.dump({"seen": dict(SEEN), "transitions": dict(TRANSITIONS)}, handle, indent=1,
                  sort_keys=True)


def check(response, *allowed, where=""):
    """Every answer: never a 500, one of the codes the step allows, and any
    4xx in RFC 9457 problem+json."""
    request = getattr(response, "request", None)
    path = request.url.path if request is not None else where
    label = where or f"{request.method} " + "/".join(
        "{id}" if p.isdigit() else p for p in path.removeprefix("/api/v1").split("/"))
    SEEN[f"{label} {response.status_code}"] += 1
    assert response.status_code < 500, (path, response.status_code, response.text)
    assert response.status_code in allowed, (path, response.status_code, allowed, response.text)
    if 400 <= response.status_code < 500:
        content_type = response.headers.get("content-type", "")
        assert content_type.startswith(PROBLEM_JSON), (path, response.status_code, content_type)
    return response


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    reset_provider()
    for name in ("send_order_confirmation_email", "send_order_status_update_email",
                 "send_order_cancellation_email"):
        monkeypatch.setattr(f"app.api.v1.orders.{name}", lambda *a, **k: True)
    monkeypatch.setattr("app.tasks.send_order_cancellation_email", lambda *a, **k: True)
    yield
    reset_provider()


def make_product(db, price, stock, name="Torture widget"):
    category = Category(name=f"cat-{uuid.uuid4().hex[:10]}", path=[], level=0)
    db.add(category)
    db.commit()
    product = Product(name=name, description="A product made by the torture suite",
                      price=Decimal(price), stock_quantity=stock, category_id=category.id,
                      images=["https://img.test/p.png"])
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def make_variant(db, product, price_override, stock):
    variant = ProductVariant(product_id=product.id, sku=f"TT-{uuid.uuid4().hex[:12]}",
                             attributes={"size": "M"},
                             price_override=None if price_override is None else Decimal(price_override),
                             stock_quantity=stock)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    return variant


def money(value) -> Decimal:
    return Decimal(str(value))


# =============================================================================
# Model 1 - cart, guest cart, merge at login, validate, checkout
# =============================================================================
#
# Merge rule (app/schemas/common.py Cart.merge, used by redis_codec
# plan_merge_into_account): for every guest line, in the guest cart's order,
# a line already in the account cart gets the quantities SUMMED (no clamp to
# stock, none to any maximum - /cart/validate reports and checkout refuses
# what exceeds stock); a new line is appended while the account cart has
# fewer than CART_MAX_LINES lines, otherwise it is DROPPED (logged). The
# guest cart is deleted and its cookie cleared. The model below implements
# exactly that and the machine compares it, line order included.

CART_MAX_LINES = 4  # patched (default 100) so merges and adds meet the cap


class CartWorld(RuleBasedStateMachine):
    shop: Shop = None

    @initialize(prices=st.lists(PRICES, min_size=3, max_size=3),
                stocks=st.lists(st.integers(2, 9) | st.just(0), min_size=5, max_size=5),
                overrides=st.lists(st.none() | PRICES, min_size=2, max_size=2))
    def setup(self, prices, stocks, overrides):
        db = self.shop.db
        products = [make_product(db, prices[i], stocks[i]) for i in range(3)]
        variants = [make_variant(db, products[2], overrides[i], stocks[3 + i]) for i in range(2)]
        # catalog model
        self.products = {p.id: {"price": money(p.price), "stock": p.stock_quantity,
                                "state": "active"} for p in products}
        self.variants = {v.id: {"product": v.product_id, "stock": v.stock_quantity,
                                "override": None if v.price_override is None else money(v.price_override),
                                "active": True} for v in variants}
        self.keys = [(products[0].id, None), (products[1].id, None), (products[2].id, None),
                     (products[2].id, variants[0].id), (products[2].id, variants[1].id)]
        self.buyer = self.shop.buyer()
        self.user_headers = self.buyer.headers
        self.admin = self.shop.admin()
        self.guest = TestClient(app, base_url="https://testserver")
        self.user_cart = {}
        self.guest_cart = None  # None: no stored guest cart (no cookie)
        self.ordered_products = set()
        self.placed = []  # [(order_id, {key: qty})] not cancelled

    # -- model helpers ---------------------------------------------------------

    def _key(self, index):
        return self.keys[index % len(self.keys)]

    def _target(self, who, index, anywhere):
        """Mostly a line that is in the cart (so update/remove do something),
        sometimes any line."""
        lines = list(self._cart(who) or {})
        if lines and not anywhere:
            return lines[index % len(lines)]
        return self._key(index) if anywhere else None

    def _unavailable(self, key):
        product_id, variant_id = key
        product = self.products[product_id]
        if product["state"] != "active":
            return "product_unavailable"
        if variant_id is not None:
            variant = self.variants[variant_id]
            if product["state"] == "gone" or not variant["active"]:
                return "variant_unavailable"
        return None

    def _stock(self, key):
        product_id, variant_id = key
        return self.variants[variant_id]["stock"] if variant_id else self.products[product_id]["stock"]

    def _set_stock(self, key, value):
        product_id, variant_id = key
        if variant_id:
            self.variants[variant_id]["stock"] = value
        else:
            self.products[product_id]["stock"] = value

    def _price(self, key):
        product_id, variant_id = key
        if variant_id and self.variants[variant_id]["override"] is not None:
            return self.variants[variant_id]["override"]
        return self.products[product_id]["price"]

    def _client(self, who):
        if who == "guest":
            return self.guest, {}
        return self.shop.client, self.user_headers

    def _cart(self, who):
        return self.guest_cart if who == "guest" else self.user_cart

    def _resolve(self, choice, key, current):
        if isinstance(choice, int):
            return choice
        stock = self._stock(key)
        return stock - current + (1 if choice == "over" else 0)

    def _expect_add(self, who, key, quantity):
        cart = self._cart(who)
        if not 1 <= quantity <= INT32_MAX:
            return 422, None
        product_id, variant_id = key
        if self.products[product_id]["state"] != "active":
            return 404, "Product not found"
        if variant_id is not None and not self.variants[variant_id]["active"]:
            return 404, "Product variant not found"
        lines = cart or {}
        if key not in lines and len(lines) >= CART_MAX_LINES:
            return 409, "Cart cannot hold"
        if self._stock(key) < lines.get(key, 0) + quantity:
            return 409, "Not enough stock"
        return 200, None

    # -- cart rules ----------------------------------------------------------------

    @rule(who=st.sampled_from(["guest", "user"]), index=st.integers(0, 4),
          choice=st.integers(1, 2))
    def add(self, who, index, choice):
        self.add_edge(who, index, choice)

    @rule(who=st.sampled_from(["guest", "user"]), index=st.integers(0, 4),
          choice=st.sampled_from(["fill", "fill", "over", 0, -1, INT32_MAX, INT32_MAX + 1]))
    def add_edge(self, who, index, choice):
        key = self._key(index)
        quantity = self._resolve(choice, key, (self._cart(who) or {}).get(key, 0))
        expected, detail = self._expect_add(who, key, quantity)
        client, headers = self._client(who)
        body = {"product_id": key[0], "quantity": quantity}
        if key[1] is not None:
            body["variant_id"] = key[1]
        response = check(client.post(f"{CART}/items", json=body, headers=headers), 200, 404, 409, 422)
        assert response.status_code == expected, (key, quantity, response.text, self._cart(who))
        if detail:
            assert detail in response.json()["detail"], response.text
        if expected == 200:
            if who == "guest":
                if self.guest_cart is None:
                    self.guest_cart = {}
                    assert self.guest.cookies.get(GUEST_COOKIE), "first add must issue the cookie"
                cart = self.guest_cart
            else:
                cart = self.user_cart
            cart[key] = cart.get(key, 0) + quantity

    @rule(who=st.sampled_from(["guest", "user"]), index=st.integers(0, 4),
          choice=st.one_of(st.integers(1, 4), st.integers(1, 4),
                           st.sampled_from(["fill", "over", 0, -1, INT32_MAX + 1])),
          anywhere=rarely(4))
    def update(self, who, index, choice, anywhere):
        key = self._target(who, index, anywhere)
        if key is None:
            return
        quantity = self._resolve(choice, key, 0)
        cart = self._cart(who)
        if not 1 <= quantity <= INT32_MAX:
            expected = 422
        elif self._unavailable(key) is not None or cart is None or key not in cart:
            expected = 404
        elif self._stock(key) < quantity:
            expected = 409
        else:
            expected = 200
        client, headers = self._client(who)
        body = {"quantity": quantity}
        if key[1] is not None:
            body["variant_id"] = key[1]
        response = check(client.put(f"{CART}/items/{key[0]}", json=body, headers=headers),
                         200, 404, 409, 422)
        assert response.status_code == expected, (key, quantity, response.text, cart)
        if expected == 200:
            cart[key] = quantity

    @rule(who=st.sampled_from(["guest", "user"]), index=st.integers(0, 7), anywhere=rarely(4))
    def remove(self, who, index, anywhere):
        key = self._target(who, index, anywhere)
        if key is None:
            return
        cart = self._cart(who)
        client, headers = self._client(who)
        params = {} if key[1] is None else {"variant_id": key[1]}
        response = check(client.delete(f"{CART}/items/{key[0]}", params=params, headers=headers),
                         200, 404)
        if cart is None or key not in cart:
            assert response.status_code == 404, response.text
            return
        assert response.status_code == 200, response.text
        del cart[key]
        if who == "guest" and not cart:
            self.guest_cart = None
            assert self.guest.cookies.get(GUEST_COOKIE) is None

    @rule(who=st.sampled_from(["guest", "user"]), act=rarely(4))
    def clear(self, who, act):
        if not act:
            return
        client, headers = self._client(who)
        check(client.delete(CART, headers=headers), 200)
        if who == "guest":
            self.guest_cart = None
            assert self.guest.cookies.get(GUEST_COOKIE) is None
        else:
            self.user_cart = {}

    @precondition(lambda self: self.guest_cart is not None)
    @rule()
    def login_merges_guest_cart(self):
        had_cart = self.guest_cart is not None
        response = check(self.guest.post(LOGIN, data={"username": self.buyer.email,
                                                      "password": PASSWORD}), 200)
        self.user_headers = {"Authorization": f"Bearer {response.json()['access_token']}"}
        if had_cart:
            for key, quantity in self.guest_cart.items():
                if key in self.user_cart:
                    self.user_cart[key] += quantity
                elif len(self.user_cart) < CART_MAX_LINES:
                    self.user_cart[key] = quantity
            self.guest_cart = None
            assert self.guest.cookies.get(GUEST_COOKIE) is None

    @rule(who=st.sampled_from(["guest", "user"]))
    def validate(self, who):
        client, headers = self._client(who)
        body = check(client.post(f"{CART}/validate", headers=headers), 200).json()
        expected = []
        for key, quantity in (self._cart(who) or {}).items():
            reason = self._unavailable(key)
            if reason is not None:
                expected.append((key[0], key[1], reason, None, None))
            elif self._stock(key) < quantity:
                expected.append((key[0], key[1], "insufficient_stock", self._stock(key), quantity))
        got = [(i["product_id"], i["variant_id"], i["code"], i["available"], i["requested"])
               for i in body["issues"]]
        assert got == expected, (got, expected)
        assert body["valid"] is (not expected)

    @precondition(lambda self: self.user_cart)
    @rule()
    def checkout(self):
        cart = dict(self.user_cart)
        expected, failing = 201, None
        if not cart:
            expected = 400
        else:
            for key, quantity in cart.items():
                if self._unavailable(key) is not None or self._stock(key) < quantity:
                    expected, failing = 409, key
                    break
        stock_before = {k: self._stock(k) for k in self.keys}
        response = check(self.shop.client.post(
            ORDERS, json={"shipping_address_id": self.buyer.address_id},
            headers={**self.user_headers, "Idempotency-Key": new_key()}), 201, 400, 409)
        assert response.status_code == expected, (response.text, cart)
        if expected == 409:
            assert f"Product {failing[0]} " in response.json()["detail"]
            return
        if expected != 201:
            return
        order = response.json()
        total = Decimal("0.00")
        lines = {}
        for item in order["items"]:
            key = (item["product_id"], item["variant_id"])
            lines[key] = item["quantity"]
            assert money(item["price_at_time"]) == self._price(key), item
            total += self._price(key) * item["quantity"]
        assert lines == cart
        assert money(order["total_amount"]) == total
        for key, quantity in cart.items():
            self._set_stock(key, stock_before[key] - quantity)
            self.ordered_products.add(key[0])
        self.placed.append((order["id"], cart))
        self.user_cart = {}  # nothing changed between the read and the removal

    @rule(act=rarely(6))
    def guest_cannot_checkout(self, act):
        if not act:
            return
        response = check(self.guest.post(ORDERS, json={"shipping_address_id": 1},
                                         headers={"Idempotency-Key": new_key()}), 401)
        assert response.status_code == 401

    @precondition(lambda self: self.placed)
    @rule(index=st.integers(0, 10))
    def cancel_order(self, index):
        order_id, lines = self.placed[index % len(self.placed)]
        overflowing = [key for key, quantity in lines.items()
                       if self._stock(key) + quantity > INT32_MAX]
        if overflowing and KNOWN_BUG_ALLOWANCES:
            # KNOWN-BUG ALLOWANCE, bug 3 (strict reproducers:
            # test_bug_restock_past_int32_max_is_a_500_on_cancel and
            # ..._aborts_the_expiry_sweep): restocking this order would carry
            # a stock that an admin set to Int32Max past it - a 500 on
            # PostgreSQL, a stock above the API's maximum on SQLite. This
            # one cancellation is not attempted; TORTURE_STRICT=1 attempts it.
            SEEN["known bug 3: overflowing cancel skipped"] += 1
            return
        self.placed.remove((order_id, lines))
        check(self.shop.client.post(f"{ORDERS}/{order_id}/cancel", headers=self.user_headers), 200)
        for key, quantity in lines.items():
            # Correct behaviour for a restock that would pass Int32Max is not
            # specified beyond "no 500, never above the API's maximum".
            self._set_stock(key, min(self._stock(key) + quantity, INT32_MAX))
        if overflowing:
            db = self.shop.db
            db.expire_all()
            for key in overflowing:
                product_id, variant_id = key
                row = db.get(ProductVariant, variant_id) if variant_id else db.get(Product, product_id)
                assert 0 <= row.stock_quantity <= INT32_MAX, (key, row.stock_quantity)
                self._set_stock(key, row.stock_quantity)

    # -- admin rules -----------------------------------------------------------------

    @rule(index=st.integers(0, 4), value=st.one_of(st.integers(0, 8), st.just(INT32_MAX)))
    def admin_sets_stock(self, index, value):
        key = self._key(index)
        product_id, variant_id = key
        url = (f"{ADMIN}/products/{product_id}" if variant_id is None
               else f"{ADMIN}/products/{product_id}/variants/{variant_id}")
        response = check(self.shop.client.put(url, json={"stock_quantity": value},
                                              headers=self.admin), 200, 404)
        if self.products[product_id]["state"] == "gone":
            assert response.status_code == 404
            return
        assert response.status_code == 200, response.text
        self._set_stock(key, value)

    @rule(index=st.integers(0, 4), price=st.none() | PRICES)
    def admin_sets_price(self, index, price):
        product_id, variant_id = self._key(index)
        if variant_id is None:
            if price is None:
                return
            url, body = f"{ADMIN}/products/{product_id}", {"price": str(price)}
        else:
            url = f"{ADMIN}/products/{product_id}/variants/{variant_id}"
            body = {"price_override": None if price is None else str(price)}
        response = check(self.shop.client.put(url, json=body, headers=self.admin), 200, 404)
        if self.products[product_id]["state"] == "gone":
            assert response.status_code == 404
            return
        assert response.status_code == 200, response.text
        if variant_id is None:
            self.products[product_id]["price"] = price
        else:
            self.variants[variant_id]["override"] = price

    @rule(which=st.integers(0, 1), active=st.sampled_from([True, False, True]))
    def admin_toggles_variant(self, which, active):
        product_id, variant_id = self.keys[3 + which]
        response = check(self.shop.client.put(
            f"{ADMIN}/products/{product_id}/variants/{variant_id}", json={"is_active": active},
            headers=self.admin), 200, 404)
        if self.products[product_id]["state"] == "gone":
            assert response.status_code == 404
            return
        assert response.status_code == 200, response.text
        self.variants[variant_id]["active"] = active

    @rule(which=st.integers(0, 2), act=rarely(8))
    def admin_deletes_product(self, which, act):
        if not act:
            return  # rare: a deleted product cannot come back
        product_id = self.keys[which][0]
        response = check(self.shop.client.delete(f"{ADMIN}/products/{product_id}",
                                                 headers=self.admin), 204, 404)
        state = self.products[product_id]
        if state["state"] == "gone":
            assert response.status_code == 404
            return
        assert response.status_code == 204, response.text
        # Soft delete when an order line references it, hard delete otherwise.
        state["state"] = "inactive" if product_id in self.ordered_products else "gone"

    # -- invariants --------------------------------------------------------------------

    def _compare(self, body, model):
        model = model or {}
        got = [(i["product_id"], i["variant_id"], i["quantity"]) for i in body["items"]]
        assert got == [(k[0], k[1], q) for k, q in model.items()], (got, model)
        total = Decimal("0.00")
        for item in body["items"]:
            key = (item["product_id"], item["variant_id"])
            assert item["quantity"] >= 1
            reason = self._unavailable(key)
            if reason is not None:
                assert item["available"] is False and item["unavailable_reason"] == reason, item
                assert item["unit_price"] is None and item["line_total"] is None, item
                continue
            assert item["available"] is True, item
            price = self._price(key)
            assert money(item["unit_price"]) == price, (item, price)
            assert money(item["line_total"]) == price * item["quantity"], item
            total += price * item["quantity"]
        assert money(body["total_amount"]) == total, (body["total_amount"], total)
        assert money(body["total_amount"]).as_tuple().exponent >= -2
        assert body["items_count"] == len(model)

    @invariant()
    def carts_and_stock_match_the_model(self):
        if not hasattr(self, "keys"):
            return
        user = check(self.shop.client.get(CART, headers=self.user_headers), 200).json()
        self._compare(user, self.user_cart)
        assert len(user["items"]) <= CART_MAX_LINES
        guest = check(self.guest.get(CART), 200).json()
        self._compare(guest, self.guest_cart)
        assert len(guest["items"]) <= CART_MAX_LINES
        db = self.shop.db
        db.expire_all()
        for key in self.keys:
            product_id, variant_id = key
            if self.products[product_id]["state"] == "gone":
                assert db.get(Product, product_id) is None
                if variant_id:
                    assert db.get(ProductVariant, variant_id) is None
                continue
            row = db.get(ProductVariant, variant_id) if variant_id else db.get(Product, product_id)
            assert row.stock_quantity >= 0
            assert row.stock_quantity == self._stock(key), (key, row.stock_quantity, self._stock(key))


def test_cart_world_matches_its_model(client, db_session, monkeypatch):
    monkeypatch.setattr(settings, "CART_MAX_LINES", CART_MAX_LINES)
    CartWorld.shop = Shop(client, db_session)
    SEEN.clear()
    try:
        run_state_machine_as_test(CartWorld, settings=hyp_settings(
            max_examples=scaled_examples(CART_WORLD_EXAMPLES), stateful_step_count=30,
            **_HYPOTHESIS))
    finally:
        CartWorld.shop = None
        _dump_stats("cart_world")
    for outcome in ("POST /cart/items 200", "POST /users/login 200", "POST /orders 201",
                    "POST /cart/validate 200"):
        assert SEEN[outcome] > 0, (outcome, SEEN)


# =============================================================================
# Model 2 - order & payment lifecycle with an out-of-order Stripe outbox
# =============================================================================
#
# Stock model (app/api/v1/orders.py create_order, app/services/orders.py
# cancel_order_and_restock): stock_quantity is the AVAILABLE count. It is
# decremented when an order is placed (whether or not it is ever paid) and
# incremented only when an order is cancelled (customer, admin, expiry).
# Payment, refunds and disputes never move stock. The requested formula
#     initial == current + reserved + sold - refunded
# therefore maps to, per stock unit (product or variant):
#     initial + admin_adjustments == current
#                                    + sum(qty of non-cancelled unpaid orders)   (reserved)
#                                    + sum(qty of non-cancelled paid/refunded orders) (sold)
# with refunded_stock == 0 by design (a refund returns money, not goods).

ALLOWED_PAYMENT_STATUS = {
    "pending": {"paid", "failed"},
    "failed": {"paid"},
    "paid": {"refunded"},
    "refunded": {"paid"},  # only through a refund that failed after it succeeded
}
ALLOWED_ATTEMPT = {
    "processing": {"requires_action", "succeeded", "failed", "unknown"},
    "requires_action": {"succeeded", "failed"},
    "unknown": {"processing", "requires_action", "succeeded", "failed"},
    "failed": {"succeeded"},
    "succeeded": set(),
}
ALLOWED_REFUND = {
    "pending": {"succeeded", "failed", "canceled", "requires_action"},
    "requires_action": {"pending", "succeeded", "failed", "canceled"},
    "succeeded": {"failed", "canceled"},
    "failed": set(),
    "canceled": set(),
}
MAX_ORDERS = 4


class PaymentWorld(RuleBasedStateMachine):
    shop: Shop = None
    orders = Bundle("orders")

    @initialize(price_a=CHARGEABLE, price_b=CHARGEABLE, override=st.none() | CHARGEABLE)
    def setup(self, price_a, price_b, override):
        reset_provider()
        self.provider = StripePaymentProvider()
        db = self.shop.db
        a = make_product(db, price_a, 12)
        b = make_product(db, price_b, 10)
        bv = make_variant(db, b, override, 6)
        self.lines = [(a.id, None), (b.id, None), (b.id, bv.id)]
        self.prices = {(a.id, None): money(price_a), (b.id, None): money(price_b),
                       (b.id, bv.id): money(override if override is not None else price_b)}
        self.initial = {(a.id, None): 12, (b.id, None): 10, (b.id, bv.id): 6}
        self.adjust = {line: 0 for line in self.lines}
        self.buyers = [self.shop.buyer(), self.shop.buyer()]
        self.admin = self.shop.admin()
        self.order_ids = []
        self.owner = {}
        self.order_keys = []  # (order_id, buyer, key)
        self.pay_keys = []  # (order_id, key, token)
        self.refund_keys = []  # (order_id, key, amount, refund id)
        self.refund_ids = []  # provider refunds made in this example
        self.outbox = []  # undelivered Stripe events: dicts(id, type, obj)
        self.delivered = []
        self.disputes = {}  # dispute id -> provider-side dispute
        self.rewound = {}  # refund id -> stale status a late event wrote over succeeded (bug 1)
        self.snap = self._snapshot()

    # -- helpers -----------------------------------------------------------------

    def _db(self):
        db = self.shop.db
        db.expire_all()
        return db

    def _order(self, order_id):
        return self._db().get(Order, order_id)

    def _payments(self, order_id):
        return self.shop.payments(order_id)

    def _intent(self, payment_intent_id):
        return StripePaymentProvider._intents_by_id.get(payment_intent_id)

    def _stock(self, line):
        db = self._db()
        product_id, variant_id = line
        row = db.get(ProductVariant, variant_id) if variant_id else db.get(Product, product_id)
        return row.stock_quantity

    def _in_flight(self, order_id):
        stale = utc_now() - timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES)
        return any(p.status in ("processing", "requires_action", "unknown")
                   and p.created_at.replace(tzinfo=None) > stale.replace(tzinfo=None)
                   for p in self._payments(order_id))

    def _open_dispute(self, order_id, payment_id=None):
        return any(d.status not in DISPUTE_CLOSED_STATUSES
                   for p in self._payments(order_id) if payment_id in (None, p.id)
                   for d in p.disputes)

    def _charge(self, order_id):
        """The unflagged succeeded attempt (the charge that paid the order)."""
        charges = [p for p in self._payments(order_id)
                   if p.status == "succeeded" and not p.requires_manual_review]
        return charges[-1] if charges else None

    def _paid_rather(self, order_id, any_order):
        """The drawn order, or (usually) a paid one if there is one: the
        refund and dispute rules have nothing to do on most unpaid orders."""
        if any_order:
            return order_id
        paid = [o for o in self.order_ids
                if self._order(o).payment_status in ("paid", "refunded")]
        return paid[order_id % len(paid)] if paid else order_id

    def _enqueue(self, event_type, obj, event_id=None):
        self.outbox.append({"id": event_id or f"evt_{uuid.uuid4().hex[:24]}",
                            "type": event_type, "obj": obj})

    def _intent_object(self, intent, order_id=None):
        obj = {"id": intent.id, "object": "payment_intent", "amount": intent.amount,
               "amount_received": intent.amount_received or 0,
               "currency": intent.currency.lower(), "status": intent.status.value}
        if order_id is not None:
            obj["metadata"] = {"order_id": str(order_id)}
        return obj

    def _refund_object(self, refund):
        return {"id": refund.id, "object": "refund", "amount": refund.amount,
                "currency": refund.currency.lower(), "payment_intent": refund.payment_intent,
                "status": refund.status.value}

    def _charge_refunded(self, payment_intent_id):
        intent = self._intent(payment_intent_id)
        refunds = [self._refund_object(r) for r in self.provider.list_refunds(payment_intent_id)]
        return {"id": f"ch_{payment_intent_id}", "object": "charge",
                "payment_intent": payment_intent_id, "amount": intent.amount,
                "amount_refunded": intent.amount_refunded, "currency": intent.currency.lower(),
                "refunds": {"object": "list", "data": refunds}}

    def _snapshot(self):
        db = self._db()
        orders = {o.id: (o.status, o.payment_status)
                  for o in db.query(Order).filter(Order.id.in_(self.order_ids or [-1]))}
        payments, refunds, disputes = {}, {}, {}
        for p in db.query(Payment).filter(Payment.order_id.in_(self.order_ids or [-1])):
            payments[p.id] = (p.status, p.requires_manual_review, p.order_id)
            for r in p.refunds:
                refunds[r.refund_id] = (r.status, p.order_id)
            for d in p.disputes:
                disputes[d.dispute_id] = d.status
        return {"orders": orders, "payments": payments, "refunds": refunds, "disputes": disputes}

    def _observe(self, why):
        """Check every change since the previous observation against the
        transition graph, then make the current state the reference."""
        before, after = self.snap, self._snapshot()
        refund_failed_for = {order_id for rid, (status, order_id) in after["refunds"].items()
                             if status in ("failed", "canceled")
                             and before["refunds"].get(rid, (None,))[0] not in ("failed", "canceled")}
        # Orders of which a refund was just rewound by a stale event (bug 1,
        # tolerated - see _deliver).
        rewound_for = {order_id for rid, (status, order_id) in after["refunds"].items()
                       if self.rewound.get(rid) == status
                       and before["refunds"].get(rid, (None,))[0] == "succeeded"}
        for order_id, (status, pay) in after["orders"].items():
            if order_id not in before["orders"]:
                assert (status, pay) == ("new", "pending"), (why, order_id, status, pay)
                continue
            status0, pay0 = before["orders"][order_id]
            if status != status0:
                TRANSITIONS[f"order {status0}->{status}"] += 1
                assert status in ORDER_STATUS_TRANSITIONS[status0], (why, order_id, status0, status)
            if pay != pay0:
                TRANSITIONS[f"payment_status {pay0}->{pay}"] += 1
                assert pay in ALLOWED_PAYMENT_STATUS[pay0], (why, order_id, pay0, pay)
                if (pay0, pay) == ("refunded", "paid") and order_id not in rewound_for:
                    assert order_id in refund_failed_for, (
                        f"{why}: order {order_id} went refunded->paid without any refund failing")
                if pay == "paid":
                    assert status0 != "cancelled", (why, "cancelled order marked paid", order_id)
        for payment_id, (status, flagged, _order) in after["payments"].items():
            if payment_id not in before["payments"]:
                continue
            status0, flagged0, _ = before["payments"][payment_id]
            if status != status0:
                TRANSITIONS[f"attempt {status0}->{status}"] += 1
                assert status in ALLOWED_ATTEMPT[status0], (why, payment_id, status0, status)
            assert not (flagged0 and not flagged), (why, "manual-review flag cleared", payment_id)
        for refund_id, (status, order_id) in after["refunds"].items():
            if refund_id in before["refunds"] and before["refunds"][refund_id][0] != status:
                status0 = before["refunds"][refund_id][0]
                TRANSITIONS[f"refund {status0}->{status}"] += 1
                if status0 == "succeeded" and self.rewound.get(refund_id) == status:
                    continue  # bug 1, tolerated (see _deliver)
                assert status in ALLOWED_REFUND[status0], (
                    why, f"refund {refund_id} of order {order_id} went {status0}->{status}")
        for dispute_id, status in after["disputes"].items():
            status0 = before["disputes"].get(dispute_id)
            if status0 is not None and status0 != status:
                TRANSITIONS[f"dispute {status0}->{status}"] += 1
                assert status0 not in DISPUTE_CLOSED_STATUSES, (why, dispute_id, status0, status)
        self.snap = after

    @staticmethod
    def _reported_refunds(obj):
        """The refund objects an event carries: a refund event's own object,
        a charge.refunded snapshot's refunds list."""
        if obj.get("object") == "refund":
            return [obj]
        refunds = obj.get("refunds")
        return list(refunds.get("data", [])) if isinstance(refunds, dict) else []

    def _note_stale_refund_reports(self, event):
        """KNOWN-BUG ALLOWANCE, bug 1 (strict reproducer:
        test_bug_stale_refund_event_reverts_a_settled_refund). Stripe does
        not order events: a snapshot taken while a refund was pending can
        arrive after the event that reported it succeeded, and the worker
        writes the stale status over "succeeded" (and may move the order
        from refunded back to paid). Exactly that is tolerated afterwards -
        the refund going from succeeded to the status this event reported,
        the order's refunded -> paid in the same step, our record differing
        from the provider's for that refund after a drain - and nothing
        else. Off under TORTURE_STRICT=1."""
        if not KNOWN_BUG_ALLOWANCES:
            return
        ours = {}
        for order_id in self.order_ids:
            for payment in self._payments(order_id):
                ours.update({r.refund_id: r.status for r in payment.refunds})
        for refund in self._reported_refunds(event["obj"]):
            stale = refund.get("status")
            if ours.get(refund.get("id")) == "succeeded" and stale in ("pending", "requires_action"):
                self.rewound[refund["id"]] = stale
                SEEN["known bug 1: stale refund report delivered"] += 1

    def _deliver(self, event, duplicate=False):
        if not duplicate:
            self._note_stale_refund_reports(event)
        response = self.shop.webhook(event["type"], event["obj"], event_id=event["id"])
        check(response, 200, where=f"webhook {event['type']}")
        route = getattr(response, "route_response", response)
        if duplicate:
            assert route.json()["status"] == "already_processed", route.text
            return
        assert route.json()["status"] == "received", route.text
        result = response.json()
        # The worker must apply (or knowingly ignore) every event: a task
        # that ends "retrying"/"failed" is the worker's 500.
        assert result.get("status") == "processed", (event["type"], result, self._last_error(event))
        self._observe(f"deliver {event['type']}")

    def _last_error(self, event):
        row = self._db().query(WebhookEvent).filter_by(event_id=event["id"]).first()
        return row and row.last_error

    # -- customers -----------------------------------------------------------------

    @precondition(lambda self: len(self.order_ids) < MAX_ORDERS)
    @rule(target=orders, buyer=st.integers(0, 1),
          lines=st.lists(st.tuples(st.integers(0, 2), st.integers(1, 5)), min_size=1, max_size=3))
    def place_order(self, buyer, lines):
        who = self.buyers[buyer]
        check(self.shop.client.delete(CART, headers=who.headers), 200)
        cart = {}
        for index, quantity in lines:
            line = self.lines[index]
            stock = self._stock(line)
            body = {"product_id": line[0], "quantity": quantity}
            if line[1] is not None:
                body["variant_id"] = line[1]
            response = self.shop.client.post(f"{CART}/items", json=body, headers=who.headers)
            check(response, 200, 409)
            fits = stock >= cart.get(line, 0) + quantity
            assert (response.status_code == 200) is fits, (response.text, stock, cart, line)
            if fits:
                cart[line] = cart.get(line, 0) + quantity
        stock_before = {line: self._stock(line) for line in self.lines}
        key = new_key()
        response = check(self.shop.place_order(who, key=key), 201, 400, 409)
        if not cart:
            assert response.status_code == 400
            return multiple()
        assert response.status_code == 201, response.text
        order = response.json()
        total = sum((self.prices[line] * q for line, q in cart.items()), Decimal("0.00"))
        assert money(order["total_amount"]) == total
        for line in self.lines:
            assert self._stock(line) == stock_before[line] - cart.get(line, 0)
        self.order_ids.append(order["id"])
        self.owner[order["id"]] = buyer
        self.order_keys.append((order["id"], buyer, key))
        self._observe("place_order")
        return order["id"]

    @precondition(lambda self: self.order_keys)
    @rule(index=st.integers(0, 10))
    def replay_order_key(self, index):
        order_id, buyer, key = self.order_keys[index % len(self.order_keys)]
        stock_before = {line: self._stock(line) for line in self.lines}
        response = check(self.shop.place_order(self.buyers[buyer], key=key), 201)
        assert response.json()["id"] == order_id
        assert {line: self._stock(line) for line in self.lines} == stock_before
        self._observe("replay_order_key")

    @rule(order_id=orders, token=st.sampled_from(
        [PAY_OK, PAY_OK, PAY_DECLINED, PAY_3DS, PAY_PENDING_REFUND, PAY_REFUND_FAIL, PAY_3DS_DECLINE]),
          stranger=st.integers(0, 9))
    def pay(self, order_id, token, stranger):
        owner = self.owner[order_id]
        who = self.buyers[1 - owner] if stranger == 0 else self.buyers[owner]
        order = self._order(order_id)
        if stranger == 0:
            expected = 404
        elif order.payment_status in ("paid", "refunded") or order.status == "cancelled" or self._in_flight(order_id):
            expected = 409
        elif token == PAY_DECLINED:
            expected = 422
        else:
            expected = 200
        key = new_key()
        response = check(self.shop.pay(who, order_id, token=token, amount=order.total_amount, key=key),
                         200, 404, 409, 422)
        assert response.status_code == expected, (token, response.text, order.status,
                                                  order.payment_status)
        if expected in (200, 422):
            self.pay_keys.append((order_id, key, token))
            attempt = self._payments(order_id)[-1]
            intent = self._intent(attempt.payment_intent_id)
            if token in SUCCEEDING:
                assert response.json()["status"] == "paid"
                self._enqueue("payment_intent.succeeded", self._intent_object(intent, order_id))
            elif token in AUTHENTICATING:
                assert response.json()["status"] == "requires_action"
            else:
                self._enqueue("payment_intent.payment_failed", self._intent_object(intent, order_id))
        self._observe("pay")

    @rule(order_id=orders)
    def pay_with_a_good_card(self, order_id):
        # The common case, drawn as often as all card variants together.
        self.pay(order_id, PAY_OK, 1)

    @precondition(lambda self: self.pay_keys)
    @rule(index=st.integers(0, 20))
    def replay_pay(self, index):
        order_id, key, token = self.pay_keys[index % len(self.pay_keys)]
        attempts = self._payments(order_id)
        attempt = next(p for p in attempts if p.idempotency_key == key)
        order = self._order(order_id)
        response = check(self.shop.pay(self.buyers[self.owner[order_id]], order_id, token=token,
                                       amount=order.total_amount, key=key), 200, 409)
        assert len(self._payments(order_id)) == len(attempts), "a replay made a new attempt"
        if attempt.status == "processing":
            assert response.status_code == 409
        else:
            assert response.status_code == 200, response.text
            assert response.json()["id"] == attempt.id
        self._observe("replay_pay")

    @rule(order_id=orders, act=rarely(2))
    def open_checkout_session(self, order_id, act):
        if not act:
            return
        order = self._order(order_id)
        blocked = (order.payment_status in ("paid", "refunded") or order.status == "cancelled"
                   or self._in_flight(order_id))
        response = check(self.shop.checkout_session(self.buyers[self.owner[order_id]], order_id),
                         200, 409)
        assert response.status_code == (409 if blocked else 200), response.text
        self._observe("checkout_session")

    @rule(order_id=orders, stranger=st.integers(0, 4), act=rarely(3))
    def customer_cancels(self, order_id, stranger, act):
        if not act:
            return
        owner = self.owner[order_id]
        order = self._order(order_id)
        if stranger == 0:
            expected = 404
        elif order.status == "new" and order.payment_status in ("pending", "failed"):
            expected = 200
        else:
            expected = 409
        who = self.buyers[1 - owner] if stranger == 0 else self.buyers[owner]
        response = check(self.shop.cancel(who, order_id), 200, 404, 409)
        assert response.status_code == expected, response.text
        self._observe("customer_cancels")

    @rule(order_id=orders, index=st.integers(0, 5), authenticate=st.booleans())
    def customer_finishes_3ds(self, order_id, index, authenticate):
        waiting = [p for p in self._payments(order_id)
                   if (i := self._intent(p.payment_intent_id)) is not None
                   and i.status == PaymentIntentStatus.REQUIRES_ACTION]
        if not waiting:
            return
        intent = self._intent(waiting[index % len(waiting)].payment_intent_id)
        if authenticate:
            try:
                self.provider.confirm_payment_intent(intent.id, return_url="https://shop.test/r")
                self._enqueue("payment_intent.succeeded", self._intent_object(intent, order_id))
            except Exception:  # CardError: declined after authentication
                self._enqueue("payment_intent.payment_failed", self._intent_object(intent, order_id))
        else:
            intent.status = PaymentIntentStatus.CANCELED
            self._enqueue("payment_intent.canceled", self._intent_object(intent, order_id))

    @rule(order_id=orders, index=st.integers(0, 5),
          outcome=st.sampled_from(["completed", "completed", "expired", "async_payment_failed"]))
    def checkout_session_outcome(self, order_id, index, outcome):
        attempts = [p for p in self._payments(order_id) if p.checkout_session_id is not None
                    and StripePaymentProvider._sessions_by_id[p.checkout_session_id].status
                    == CheckoutSessionStatus.OPEN]
        if not attempts:
            return
        payment = attempts[index % len(attempts)]
        session = StripePaymentProvider._sessions_by_id[payment.checkout_session_id]
        base = {"id": session.id, "object": "checkout.session", "amount_total": session.amount_total,
                "currency": session.currency.lower(), "metadata": {"order_id": str(order_id)}}
        if outcome == "completed":
            intent = self.provider.create_payment_intent(
                amount=session.amount_total, currency=session.currency,
                idempotency_key=f"cs-{session.id}", payment_method=PAY_OK)
            self.provider.confirm_payment_intent(intent.id, return_url="https://shop.test/r")
            session.status = CheckoutSessionStatus.COMPLETE
            session.payment_intent = intent.id
            self._enqueue("checkout.session.completed",
                          {**base, "payment_status": "paid", "payment_intent": intent.id})
            self._enqueue("payment_intent.succeeded", self._intent_object(intent))
        elif outcome == "expired":
            session.status = CheckoutSessionStatus.EXPIRED
            self._enqueue("checkout.session.expired", {**base, "payment_status": "unpaid"})
        else:
            session.status = CheckoutSessionStatus.COMPLETE
            self._enqueue("checkout.session.completed", {**base, "payment_status": "unpaid"})
            self._enqueue("checkout.session.async_payment_failed", {**base, "payment_status": "unpaid"})

    # -- Stripe event delivery ---------------------------------------------------------

    @precondition(lambda self: self.outbox)
    @rule(index=st.integers(0, 50))
    def deliver_one_event(self, index):
        event = self.outbox.pop(index % len(self.outbox))
        self._deliver(event)
        self.delivered.append(event)

    @precondition(lambda self: self.delivered)
    @rule(index=st.integers(0, 50))
    def redeliver_an_event(self, index):
        before = self._snapshot()
        self._deliver(self.delivered[index % len(self.delivered)], duplicate=True)
        assert self._snapshot() == before, "a redelivered event changed state"

    @precondition(lambda self: self.outbox)
    @rule(seed=st.integers(0, 2 ** 16))
    def drain_outbox_in_any_order(self, seed):
        events, self.outbox = self.outbox, []
        random.Random(seed).shuffle(events)
        for event in events:
            self._deliver(event)
            self.delivered.append(event)
        self._converged()

    def _converged(self):
        """Every event delivered: our records must say what the provider says."""
        for order_id in self.order_ids:
            for payment in self._payments(order_id):
                intent = self._intent(payment.payment_intent_id)
                if intent is None:
                    continue
                provider_refunds = {r.id: r for r in self.provider.list_refunds(intent.id)}
                ours = {r.refund_id: r for r in payment.refunds}
                assert set(provider_refunds) == set(ours), (order_id, provider_refunds, ours)
                for refund_id, refund in provider_refunds.items():
                    if (refund.status.value == "succeeded"
                            and self.rewound.get(refund_id) == ours[refund_id].status):
                        continue  # bug 1, tolerated (see _note_stale_refund_reports)
                    assert ours[refund_id].status == refund.status.value, (
                        f"order {order_id}: refund {refund_id} is {ours[refund_id].status} here, "
                        f"{refund.status.value} at the provider (all events delivered)")
                assert payment.amount_refunded == intent.amount_refunded, (
                    order_id, payment.amount_refunded, intent.amount_refunded)
                for dispute in payment.disputes:
                    assert dispute.status == self.disputes[dispute.dispute_id]["status"]
            charge = self._charge(order_id)
            order = self._order(order_id)
            if charge is not None and order.payment_status in ("paid", "refunded"):
                intent = self._intent(charge.payment_intent_id)
                returned = sum(r.amount for r in self.provider.list_refunds(intent.id)
                               if r.status.value == "succeeded")
                lost = any(d["status"] == "lost" and d["pi"] == intent.id
                           for d in self.disputes.values())
                expected = "refunded" if lost or returned >= intent.amount else "paid"
                if any(r.refund_id in self.rewound for r in charge.refunds):
                    # bug 1, tolerated: the order follows our (rewound) record.
                    ours = sum(r.amount for r in charge.refunds if r.status == "succeeded")
                    expected = {expected, "refunded" if lost or ours >= charge.amount else "paid"}
                else:
                    expected = {expected}
                assert order.payment_status in expected, (order_id, order.payment_status, expected)

    # -- Stripe-side actions -----------------------------------------------------------

    @rule(order_id=orders, amount=st.integers(1, 6000), any_order=rarely(4))
    def dashboard_refund(self, order_id, amount, any_order):
        order_id = self._paid_rather(order_id, any_order)
        succeeded = [p for p in self._payments(order_id) if p.status == "succeeded"
                     and self._intent(p.payment_intent_id) is not None]
        if not succeeded:
            return
        intent = self._intent(succeeded[0].payment_intent_id)
        if amount > intent.amount - intent.amount_refunded:
            return  # Stripe refuses it
        refund = self.provider.create_refund(payment_intent_id=intent.id, amount=amount)
        self.refund_ids.append(refund.id)
        self._enqueue("refund.created", self._refund_object(refund))
        self._enqueue("charge.refunded", self._charge_refunded(intent.id))

    @precondition(lambda self: self.refund_ids)
    @rule(index=st.integers(0, 20))
    def stripe_settles_a_refund(self, index):
        refund = StripePaymentProvider._refunds_by_id[self.refund_ids[index % len(self.refund_ids)]]
        try:
            event = StripePaymentProvider.settle_refund(refund.id)
        except ValueError:
            return  # nothing left to settle
        self._enqueue(event["type"], event["data"]["object"], event_id=event["id"])

    @rule(order_id=orders,
          action=st.sampled_from(["open", "update", "won", "lost", "warning_closed"]),
          any_order=rarely(4))
    def dispute(self, order_id, action, any_order):
        order_id = self._paid_rather(order_id, any_order)
        succeeded = [p for p in self._payments(order_id) if p.status == "succeeded"
                     and self._intent(p.payment_intent_id) is not None]
        if not succeeded:
            return
        intent = self._intent(succeeded[0].payment_intent_id)
        dispute_id = f"du_{intent.id}"
        current = self.disputes.get(dispute_id)
        if action == "open":
            disputable = intent.amount - intent.amount_refunded
            if current is not None or disputable <= 0:
                return
            current = self.disputes[dispute_id] = {"pi": intent.id, "amount": disputable,
                                                   "status": "needs_response"}
            event_type = "charge.dispute.created"
        else:
            if current is None or current["status"] in DISPUTE_CLOSED_STATUSES:
                return
            current["status"] = "under_review" if action == "update" else action
            event_type = ("charge.dispute.updated" if action == "update"
                          else "charge.dispute.closed")
        self._enqueue(event_type, {"id": dispute_id, "object": "dispute",
                                   "amount": current["amount"], "currency": intent.currency.lower(),
                                   "payment_intent": intent.id, "status": current["status"],
                                   "reason": "fraudulent"})

    # -- admins and time ------------------------------------------------------------

    @rule(order_id=orders, amount=st.none() | st.integers(1, 6000), any_order=rarely(4))
    def admin_refunds(self, order_id, amount, any_order):
        order_id = self._paid_rather(order_id, any_order)
        order = self._order(order_id)
        charge = self._charge(order_id)
        if order.payment_status != "paid" or charge is None or self._open_dispute(order_id, charge.id):
            expected = 409
        else:
            intent = self._intent(charge.payment_intent_id)
            refundable = intent.amount - intent.amount_refunded
            wanted = refundable if amount is None else amount
            expected = 200 if 0 < wanted <= refundable else 400
            if (expected == 200 and KNOWN_BUG_ALLOWANCES
                    and charge.amount_refunded + wanted > charge.amount):
                # KNOWN-BUG ALLOWANCE, bug 2 (strict reproducer:
                # test_bug_refund_after_an_unreported_refund_failure_is_a_500):
                # a refund that failed at the provider, whose refund.failed is
                # still in the outbox, counts in our amount_refunded; the
                # provider would accept this refund and the route answer 500.
                # Not attempted; TORTURE_STRICT=1 attempts it.
                SEEN["known bug 2: refund over an unreported failure skipped"] += 1
                return
        value = None if amount is None else Decimal(amount) / 100
        key = new_key()
        provider_refunds = set(StripePaymentProvider._refunds_by_id)
        response = check(self.shop.refund(self.admin, order_id, amount=value, key=key),
                         200, 400, 409)
        # Precondition state only from what this example did (never class
        # state read directly): a refund the provider made counts even if the
        # route then failed.
        self.refund_ids.extend(sorted(set(StripePaymentProvider._refunds_by_id) - provider_refunds))
        assert response.status_code == expected, (response.text, order.payment_status)
        if expected == 200:
            body = response.json()
            assert cents(body["amount"]) == wanted
            refund = StripePaymentProvider._refunds_by_id[body["id"]]
            self.refund_keys.append((order_id, key, value, body["id"]))
            self._enqueue("refund.created", self._refund_object(refund))
            self._enqueue("charge.refunded", self._charge_refunded(intent.id))
        self._observe("admin_refunds")

    @precondition(lambda self: self.refund_keys)
    @rule(index=st.integers(0, 20))
    def admin_replays_a_refund(self, index):
        order_id, key, value, refund_id = self.refund_keys[index % len(self.refund_keys)]
        order = self._order(order_id)
        before = {r.refund_id for p in self._payments(order_id) for r in p.refunds}
        response = check(self.shop.refund(self.admin, order_id, amount=value, key=key),
                         200, 409)
        if order.payment_status == "paid" and not self._open_dispute(order_id):
            assert response.status_code == 200, response.text
            assert response.json()["id"] == refund_id
        after = {r.refund_id for p in self._payments(order_id) for r in p.refunds}
        assert after == before, "a refund replay recorded a new refund"
        self._observe("admin_replays_a_refund")

    @rule(order_id=orders,
          status=st.sampled_from(["confirmed", "processing", "sent", "delivered", "processing",
                                  "sent", "cancelled"]))
    def admin_sets_status(self, order_id, status):
        order = self._order(order_id)
        if status != "cancelled" and self._open_dispute(order_id) or status not in ORDER_STATUS_TRANSITIONS[order.status] or status != "cancelled" and order.payment_status != "paid":
            expected = 409
        else:
            expected = 200
        response = check(self.shop.set_status(self.admin, order_id, status), 200, 409)
        assert response.status_code == expected, (order.status, order.payment_status, status,
                                                  response.text)
        self._observe("admin_sets_status")

    @rule(order_id=orders, act=rarely(3))
    def unpaid_order_expires(self, order_id, act):
        if not act:
            return
        order = self._order(order_id)
        expires = order.status == "new" and order.payment_status in ("pending", "failed")
        order.created_at = utc_now() - timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES + 1)
        self.shop.db.commit()
        tasks.cancel_expired_unpaid_orders()
        assert (self._order(order_id).status == "cancelled") is (expires or order.status == "cancelled")
        self._observe("expiry")

    @rule(index=st.integers(0, 2), value=st.integers(0, 15), act=rarely(6))
    def admin_sets_stock(self, index, value, act):
        if not act:
            return
        line = self.lines[index]
        before = self._stock(line)
        url = (f"{ADMIN}/products/{line[0]}" if line[1] is None
               else f"{ADMIN}/products/{line[0]}/variants/{line[1]}")
        check(self.shop.client.put(url, json={"stock_quantity": value}, headers=self.admin), 200)
        self.adjust[line] += value - before

    # -- invariants ---------------------------------------------------------------------

    @invariant()
    def stock_is_conserved(self):
        if not hasattr(self, "lines"):
            return
        db = self._db()
        held = {line: 0 for line in self.lines}
        for order_id in self.order_ids:
            order = db.get(Order, order_id)
            if order.status == "cancelled":
                continue
            for item in order.items:
                held[(item.product_id, item.variant_id)] += item.quantity
        for line in self.lines:
            current = self._stock(line)
            assert current >= 0, (line, current)
            assert self.initial[line] + self.adjust[line] == current + held[line], (
                line, self.initial[line], self.adjust[line], current, held[line])

    @invariant()
    def money_is_consistent(self):
        if not hasattr(self, "lines"):
            return
        db = self._db()
        for order_id in self.order_ids:
            order = db.get(Order, order_id)
            assert order.status in ORDER_STATUS_TRANSITIONS
            assert order.payment_status in ALLOWED_PAYMENT_STATUS
            items_total = sum((i.price_at_time * i.quantity for i in order.items), Decimal("0"))
            assert order.total_amount == items_total, (order.total_amount, items_total)
            payments = self._payments(order_id)
            captured = 0
            for payment in payments:
                assert payment.status in ALLOWED_ATTEMPT, payment.status
                assert payment.currency == order.currency
                refunds = payment.refunds
                live = sum(r.amount for r in refunds if r.status not in ("failed", "canceled"))
                assert payment.amount_refunded == live, (payment.amount_refunded, live)
                assert 0 <= payment.amount_refunded <= payment.amount
                for refund in refunds:
                    assert refund.amount > 0 and refund.currency == payment.currency
                    assert refund.status in ALLOWED_REFUND, refund.status
                if payment.status != "succeeded":
                    assert payment.amount_refunded == 0 and not refunds
                else:
                    captured += payment.amount
            returned = sum(r.amount for p in payments for r in p.refunds
                           if r.status not in ("failed", "canceled"))
            assert returned <= captured, (order_id, returned, captured)
            applied = [p for p in payments if p.status == "succeeded" and not p.requires_manual_review]
            assert len(applied) <= 1, [(p.id, p.status) for p in payments]
            if order.payment_status in ("paid", "refunded"):
                assert len(applied) == 1, (order.payment_status, payments)
                charge = applied[0]
                assert charge.amount == cents(order.total_amount)
                succeeded = sum(r.amount for r in charge.refunds if r.status == "succeeded")
                lost = any(d.status == "lost" for d in charge.disputes)
                if order.payment_status == "refunded":
                    assert lost or succeeded >= charge.amount, (succeeded, charge.amount)
                else:
                    assert not lost and succeeded < charge.amount, (succeeded, charge.amount)
            else:
                assert applied == [], (order.payment_status, [(p.status, p.requires_manual_review)
                                                              for p in payments])
            if order.status in ("confirmed", "processing", "sent", "delivered"):
                assert order.payment_status in ("paid", "refunded"), (order.status, order.payment_status)

    @invariant()
    def transitions_are_legal(self):
        if hasattr(self, "snap"):
            self._observe("invariant")


def _run_payment_world(shop, examples, steps):
    PaymentWorld.shop = shop
    try:
        run_state_machine_as_test(PaymentWorld, settings=hyp_settings(
            max_examples=examples, stateful_step_count=steps, **_HYPOTHESIS))
    finally:
        PaymentWorld.shop = None


def test_payment_world_keeps_its_invariants(client, db_session, tasks_db):
    SEEN.clear()
    TRANSITIONS.clear()
    try:
        _run_payment_world(Shop(client, db_session), scaled_examples(PAYMENT_WORLD_EXAMPLES), 50)
    finally:
        _dump_stats("payment_world")
    for outcome in ("POST /orders 201", "POST /orders/{id}/pay 200",
                    "POST /orders/{id}/refund 200"):
        assert SEEN[outcome] > 0, (outcome, SEEN)


# =============================================================================
# @given properties (GIVEN_EXAMPLES each, 300 with TORTURE_FULL=1; one catalog
# per test, fresh buyers per example, so examples never share an entity)
# =============================================================================

GIVEN = dict(max_examples=scaled_examples(GIVEN_EXAMPLES), **_HYPOTHESIS)


def test_merge_at_login_sums_overlaps_appends_and_drops_beyond_the_cap(client, db_session,
                                                                        monkeypatch):
    """Merge semantics over generated carts: overlaps summed (above stock
    allowed), guest-only lines appended in guest order up to CART_MAX_LINES,
    the rest dropped; the guest cart and its cookie are gone."""
    monkeypatch.setattr(settings, "CART_MAX_LINES", 3)
    shop = Shop(client, db_session)
    products = [make_product(db_session, "1.25", 50) for _ in range(5)]
    lines = st.dictionaries(st.integers(0, 4), st.integers(1, 7), max_size=3)

    @hyp_settings(**GIVEN)
    @given(guest_lines=lines, user_lines=lines)
    def merge(guest_lines, user_lines):
        buyer = shop.buyer()
        guest = TestClient(app, base_url="https://testserver")
        for index, quantity in user_lines.items():
            check(shop.add_to_cart(buyer, products[index], quantity), 200)
        for index, quantity in guest_lines.items():
            check(guest.post(f"{CART}/items", json={"product_id": products[index].id,
                                                    "quantity": quantity}), 200)
        login = check(guest.post(LOGIN, data={"username": buyer.email, "password": PASSWORD}), 200)
        expected = {products[i].id: q for i, q in user_lines.items()}
        for index, quantity in guest_lines.items():
            product_id = products[index].id
            if product_id in expected:
                expected[product_id] += quantity
            elif len(expected) < 3:
                expected[product_id] = quantity
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        cart = check(client.get(CART, headers=headers), 200).json()
        assert [(i["product_id"], i["quantity"]) for i in cart["items"]] == list(expected.items())
        assert money(cart["total_amount"]) == Decimal("1.25") * sum(expected.values())
        assert guest.cookies.get(GUEST_COOKIE) is None
        assert check(guest.get(CART), 200).json()["items"] == []

    merge()


def test_checkout_never_sells_more_than_the_stock(client, db_session):
    """Cart quantities put in while stock was plentiful, stock then lowered
    (as by an admin or another checkout): checkout is all or nothing, never
    below zero, and decrements exactly what it sold."""
    shop = Shop(client, db_session)
    product_a = make_product(db_session, "3.10", 0)
    product_b = make_product(db_session, "0.99", 0)
    variant = make_variant(db_session, product_b, "4.44", 0)
    rows = [(Product, product_a.id), (Product, product_b.id), (ProductVariant, variant.id)]

    @hyp_settings(**GIVEN)
    @given(wanted=st.lists(st.integers(0, 6), min_size=3, max_size=3),
           stock=st.lists(st.integers(0, 6), min_size=3, max_size=3))
    def checkout(wanted, stock):
        buyer = shop.buyer()
        for model, row_id in rows:
            db_session.get(model, row_id).stock_quantity = 10
        db_session.commit()
        for index, quantity in enumerate(wanted):
            if quantity:
                check(shop.add_to_cart(buyer, product_b if index else product_a, quantity,
                                       variant=variant if index == 2 else None), 200)
        for (model, row_id), value in zip(rows, stock, strict=True):
            db_session.get(model, row_id).stock_quantity = value
        db_session.commit()
        response = check(shop.place_order(buyer), 201, 400, 409)
        db_session.expire_all()
        after = [db_session.get(model, row_id).stock_quantity for model, row_id in rows]
        if not any(wanted):
            assert response.status_code == 400
            assert after == stock
        elif any(w > s for w, s in zip(wanted, stock, strict=True)):
            assert response.status_code == 409, response.text
            assert after == stock
        else:
            assert response.status_code == 201, response.text
            assert after == [s - w for s, w in zip(stock, wanted, strict=True)]
        assert min(after) >= 0

    checkout()


def test_admin_refunds_never_exceed_the_charge(client, db_session):
    """Any sequence of partial/full refunds: accepted ones sum to at most the
    charge, exactly in cents; the order is refunded exactly when they cover it."""
    shop = Shop(client, db_session)
    product = make_product(db_session, "12.34", 100000)
    admin = shop.admin()

    @hyp_settings(**GIVEN)
    @given(quantity=st.integers(1, 3),
           amounts=st.lists(st.none() | st.integers(1, 4000), min_size=1, max_size=5))
    def refunds(quantity, amounts):
        buyer = shop.buyer()
        order = shop.order_of(buyer, product, quantity)
        check(shop.pay(buyer, order["id"]), 200)
        charge = cents(order["total_amount"])
        refunded = 0
        for amount in amounts:
            value = None if amount is None else Decimal(amount) / 100
            response = check(shop.refund(admin, order["id"], amount=value), 200, 400, 409)
            left = charge - refunded
            wanted = left if amount is None else amount
            if left == 0:
                assert response.status_code == 409
            elif wanted <= left:
                assert response.status_code == 200, response.text
                assert cents(response.json()["amount"]) == wanted
                refunded += wanted
            else:
                assert response.status_code == 400, response.text
            payment = shop.payments(order["id"])[-1]
            assert payment.amount_refunded == refunded <= payment.amount
        assert shop.order(order["id"]).payment_status == ("refunded" if refunded == charge else "paid")

    refunds()


def test_refund_events_in_any_order_converge_to_the_providers_state(client, db_session, tasks_db):
    """Refunds of a charge whose refunds settle asynchronously
    (pm_card_pendingRefund: pending, later succeeded) or fail later
    (pm_card_refundFail: succeeded, later failed), with every event Stripe
    sends about them (refund.created, charge.refunded, refund.updated /
    refund.failed) delivered in ANY order, some twice. Stripe does not
    guarantee ordering; once all are delivered our ledger must say what the
    provider says, and the order's payment_status must follow."""
    shop = Shop(client, db_session)
    product = make_product(db_session, "10.00", 100000)
    admin = shop.admin()
    provider = StripePaymentProvider()

    @hyp_settings(**GIVEN)
    @given(token=st.sampled_from([PAY_PENDING_REFUND, PAY_REFUND_FAIL]),
           parts=st.lists(st.integers(1, 1000), min_size=1, max_size=3),
           settle=st.lists(st.booleans(), min_size=3, max_size=3),
           order_seed=st.randoms(use_true_random=False),
           duplicates=st.lists(st.integers(0, 20), max_size=3))
    def converge(token, parts, settle, order_seed, duplicates):
        buyer = shop.buyer()
        order = shop.order_of(buyer, product, 1)
        check(shop.pay(buyer, order["id"], token=token), 200)
        intent_id = shop.payments(order["id"])[-1].payment_intent_id
        events = []
        for part, settles in zip(parts, settle, strict=False):
            payment = shop.payments(order["id"])[-1]
            intent = StripePaymentProvider._intents_by_id[intent_id]
            if (KNOWN_BUG_ALLOWANCES and part <= intent.amount - intent.amount_refunded
                    and payment.amount_refunded + part > payment.amount
                    and shop.order(order["id"]).payment_status == "paid"):
                # KNOWN-BUG ALLOWANCE, bug 2 (strict reproducer:
                # test_bug_refund_after_an_unreported_refund_failure_is_a_500):
                # an earlier refund failed at the provider, its refund.failed
                # is not delivered yet; the provider would take this refund
                # and the route answer 500. Not attempted (TORTURE_STRICT=1
                # attempts it).
                continue
            response = check(shop.refund(admin, order["id"], amount=Decimal(part) / 100),
                             200, 400, 409)
            if response.status_code != 200:
                continue
            refund = StripePaymentProvider._refunds_by_id[response.json()["id"]]
            snapshot = {"id": refund.id, "object": "refund", "amount": refund.amount,
                        "currency": "usd", "payment_intent": intent_id,
                        "status": refund.status.value}
            events.append(("refund.created", snapshot, None))
            events.append(("charge.refunded", {
                "id": f"ch_{intent_id}", "object": "charge", "payment_intent": intent_id,
                "refunds": {"data": [{"id": r.id, "object": "refund", "amount": r.amount,
                                      "currency": "usd", "payment_intent": intent_id,
                                      "status": r.status.value}
                                     for r in provider.list_refunds(intent_id)]}}, None))
            if settles:
                event = StripePaymentProvider.settle_refund(refund.id)
                events.append((event["type"], event["data"]["object"], None))
        events = [(t, o, f"evt_{uuid.uuid4().hex[:24]}") for t, o, _ in events]
        order_seed.shuffle(events)
        rewound = {}  # refund id -> stale status delivered after it succeeded (bug 1)
        for event_type, obj, event_id in events:
            if KNOWN_BUG_ALLOWANCES:
                ours = {r.refund_id: r.status for r in shop.payments(order["id"])[-1].refunds}
                for reported in PaymentWorld._reported_refunds(obj):
                    if (ours.get(reported["id"]) == "succeeded"
                            and reported["status"] in ("pending", "requires_action")):
                        rewound[reported["id"]] = reported["status"]
            response = shop.webhook(event_type, obj, event_id=event_id)
            check(response, 200, where=f"webhook {event_type}")
            assert response.json()["status"] == "processed", response.json()
        for index in duplicates:
            if events:
                event_type, obj, event_id = events[index % len(events)]
                again = shop.webhook(event_type, obj, event_id=event_id)
                assert again.json()["status"] == "already_processed"
        payment = shop.payments(order["id"])[-1]
        provider_refunds = {r.id: r.status.value for r in provider.list_refunds(intent_id)}
        ours = {r.refund_id: r.status for r in payment.refunds}
        tolerated = {rid for rid, status in ours.items()
                     if provider_refunds.get(rid) == "succeeded" and rewound.get(rid) == status}
        # KNOWN-BUG ALLOWANCE, bug 1 (strict reproducer:
        # test_bug_stale_refund_event_reverts_a_settled_refund): a refund that
        # a stale pending snapshot, delivered after the event that settled
        # it, rewound - and only that - may differ from the provider's.
        assert {rid: s for rid, s in ours.items() if rid not in tolerated} == {
            rid: s for rid, s in provider_refunds.items() if rid not in tolerated}
        assert set(ours) == set(provider_refunds)
        intent = StripePaymentProvider._intents_by_id[intent_id]
        assert payment.amount_refunded == intent.amount_refunded
        returned = sum(r.amount for r in provider.list_refunds(intent_id)
                       if r.status.value == "succeeded")
        expected = {"refunded" if returned >= payment.amount else "paid"}
        if tolerated:
            ours_returned = sum(r.amount for r in payment.refunds if r.status == "succeeded")
            expected.add("refunded" if ours_returned >= payment.amount else "paid")
        assert shop.order(order["id"]).payment_status in expected

    converge()


# =============================================================================
# Deterministic regressions of what the properties above found (minimal
# counterexamples, replayed without Hypothesis). They assert the CORRECT
# behaviour and are marked xfail(strict=True) until the backend is fixed:
# the fix makes them XPASS, which strict_xfail reports as a failure, so the
# marker has to go with the fix.
# =============================================================================


def _paid_order(shop, token, price="10.00"):
    product = make_product(shop.db, price, 5)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product, 1)
    check(shop.pay(buyer, order["id"], token=token), 200)
    return order["id"], shop.payments(order["id"])[-1].payment_intent_id


@pytest.mark.xfail(strict=True, reason=(
    "BUG DISCOVERED (bug 1): a stale refund.created/charge.refunded snapshot (status pending) "
    "delivered after refund.updated (succeeded) rewinds the refund succeeded->pending and the "
    "order refunded->paid - _apply_refund_object treats only failed/canceled as final "
    "(app/services/stripe_events.py:253-255), _settle_refunds recomputes the order "
    "(app/api/v1/orders.py:1771)"))
def test_bug_stale_refund_event_reverts_a_settled_refund(client, db_session, tasks_db):
    """Found by test_refund_events_in_any_order_converge_to_the_providers_state
    (token=pm_card_pendingRefund, one refund, settled, events shuffled).

    A full refund is created pending; Stripe sends refund.created (status
    pending) and, once it settles, refund.updated (status succeeded). Stripe
    does not guarantee delivery order. When refund.created arrives last, the
    worker writes the stale "pending" over "succeeded"
    (app/services/stripe_events.py:255) and _settle_refunds moves the order
    from refunded back to paid: an order whose money has been returned can
    then be moved on (processing/sent) by an admin."""
    shop = Shop(client, db_session)
    admin = shop.admin()
    order_id, intent_id = _paid_order(shop, PAY_PENDING_REFUND)
    refund = check(shop.refund(admin, order_id), 200).json()
    assert refund["status"] == "pending"
    created = {"id": refund["id"], "object": "refund", "amount": 1000, "currency": "usd",
               "payment_intent": intent_id, "status": "pending"}
    updated = StripePaymentProvider.settle_refund(refund["id"])

    check(shop.webhook(updated["type"], updated["data"]["object"]), 200)
    assert shop.order(order_id).payment_status == "refunded"
    check(shop.webhook("refund.created", created), 200)  # the older event, delivered late

    payment = shop.payments(order_id)[-1]
    assert [r.status for r in payment.refunds] == ["succeeded"], "stale event rewound the refund"
    assert shop.order(order_id).payment_status == "refunded"


@pytest.mark.xfail(strict=True, reason=(
    "BUG DISCOVERED (bug 2): /refund answers 500 for a refund the provider already made when an "
    "earlier refund failed at the provider and its refund.failed is not processed yet - "
    "_record_refund adds to a stale amount_refunded (app/api/v1/orders.py:1767, called at "
    ":1625) and violates ck_payment_refunded_within_amount"))
def test_bug_refund_after_an_unreported_refund_failure_is_a_500(client, db_session, tasks_db):
    """Found by test_refund_events_in_any_order_converge_to_the_providers_state
    (token=pm_card_refundFail, parts=[1, 1000], the first refund settled).

    A 0.01 refund succeeds, then fails at Stripe (the money is refundable
    again there); its refund.failed webhook has not been processed yet. An
    admin refunds the full 10.00: Stripe accepts it (refundable is 10.00
    again) and returns the money, but /refund adds 1000 to an
    amount_refunded that still counts the failed 1 cent
    (app/api/v1/orders.py:1767 via :1625), breaks
    ck_payment_refunded_within_amount and answers 500 - for a refund the
    provider has already made, which is then not recorded."""
    shop = Shop(client, db_session)
    admin = shop.admin()
    order_id, intent_id = _paid_order(shop, PAY_REFUND_FAIL)
    first = check(shop.refund(admin, order_id, amount=Decimal("0.01")), 200).json()
    StripePaymentProvider.settle_refund(first["id"])  # fails at Stripe; webhook still queued

    response = check(shop.refund(admin, order_id, amount=Decimal("10.00")), 200, 400, 409)

    assert response.status_code == 200, response.text
    recorded = {r.refund_id for r in shop.payments(order_id)[-1].refunds}
    assert response.json()["id"] in recorded


def _order_at_int32_max_stock(shop, admin):
    """An unpaid order of 1 unit, after which an admin sets the product's stock
    to the largest value the API accepts (Int32Max)."""
    product = make_product(shop.db, "0.00", 2)
    buyer = shop.buyer()
    order = shop.order_of(buyer, product, 1)
    check(shop.client.put(f"{ADMIN}/products/{product.id}", json={"stock_quantity": INT32_MAX},
                          headers=admin), 200)
    return product, buyer, order["id"]


@pytest.mark.xfail(strict=True, reason=(
    "BUG DISCOVERED (bug 3): restocking a cancelled order onto a stock an admin set to Int32Max "
    "(accepted by app/schemas/product.py:83,193) overflows - unchecked stock + qty in "
    "cancel_order_and_restock (app/services/orders.py:123,130): 500 NumericValueOutOfRange on "
    "PostgreSQL, stock 2147483648 (above the API's maximum) on SQLite"))
def test_bug_restock_past_int32_max_is_a_500_on_cancel(client, db_session, tasks_db):
    """Found by test_cart_world_matches_its_model on PostgreSQL:
    setup(stocks=[2,...]), add 1, login, checkout, admin_sets_stock(INT32_MAX),
    cancel_order -> 500.

    PUT /admin/products/{id} accepts stock_quantity up to 2147483647
    (app/schemas/product.py:83, Int32Max). Cancelling an order then adds its
    quantity back with an unchecked relative UPDATE
    (app/services/orders.py:123 and :130, cancel_order_and_restock): on PostgreSQL
    the INTEGER column overflows (NumericValueOutOfRange) and the customer's
    cancel answers 500 - the order can never be cancelled. SQLite has no
    32-bit column: the cancel succeeds but leaves stock 2147483648, above
    the API's own maximum, so the second assertion fails there."""
    shop = Shop(client, db_session)
    admin = shop.admin()
    product, buyer, order_id = _order_at_int32_max_stock(shop, admin)

    response = check(shop.cancel(buyer, order_id), 200, 409)

    assert response.status_code == 200, response.text
    assert 0 <= shop.stock(product) <= INT32_MAX


@pytest.mark.xfail(IS_POSTGRES, strict=True, reason=(
    "BUG DISCOVERED (bug 3, PostgreSQL): the unpaid-order expiry sweep (app/tasks.py:499 -> "
    "cancel_order_and_restock, app/services/orders.py:123,130) raises NumericValueOutOfRange "
    "on an order whose restock passes Int32Max, aborting the sweep for every other order"))
def test_bug_restock_past_int32_max_aborts_the_expiry_sweep(client, db_session, tasks_db):
    """Same overflow in the unpaid-order expiry (app/tasks.py:499 ->
    cancel_order_and_restock): on PostgreSQL the task raises, so the sweep
    stops and every other expired order in that run is left holding stock.
    (On SQLite the sweep completes - the overflow shows only as the stored
    value, covered by the test above - so it is xfail on PostgreSQL only.)"""
    shop = Shop(client, db_session)
    admin = shop.admin()
    _, _, stuck = _order_at_int32_max_stock(shop, admin)
    other_product = make_product(db_session, "1.00", 3)
    other = shop.order_of(shop.buyer(), other_product, 1)["id"]

    shop.expire_unpaid(stuck, other)  # must not raise

    assert shop.order(other).status == "cancelled"
    assert shop.stock(other_product) == 3
