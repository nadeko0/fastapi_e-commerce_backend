"""
A stateful property-based test of the payment chain: hypothesis drives the
HTTP API (TestClient, mock provider) through random sequences of what
customers, admins, Stripe and the expiry task do to a handful of orders,
and checks the invariants below after every step.

Rules (each answers one of the codes the route documents; never a 500):
create an order; /pay with a succeeding, a declined or a 3D Secure card;
the provider's verdict on an attempt by webhook (succeeded - with the
right amount or not -, payment_failed, canceled); a Checkout Session and
its completion, expiry or async failure; a refund of any amount (or of
the rest); a refund made in the Stripe Dashboard and a refund failing
after the fact; a dispute opened and closed; the customer cancelling; an
admin cancelling or moving the order on; the unpaid-order expiry.

Stripe's side is kept consistent with what Stripe would do: a webhook
only reports what happened at the (mock) provider, which is updated to
match (3D Secure completed, intent canceled, session completed or
expired, refund failed), and impossible reports (a failure for an intent
that succeeded) are not sent.

Invariants (checked after every rule, for every order):
- paid only with exactly one succeeded, unflagged attempt whose amount
  is the order total in cents and whose currency is the order's;
- refunded only with such an attempt whose succeeded refunds cover it,
  or whose dispute was lost;
- every attempt: 0 <= amount_refunded <= amount, equal to the sum of its
  recorded refunds that have not failed, and equal to what the provider
  says it refunded; an attempt that did not succeed has refunded nothing;
- stock = initial stock - the quantities of the orders not cancelled
  (stock is taken at checkout and returned only by cancellation; refunds
  and expiry of a paid order never move it);
- an order on hold (open dispute) or cancelled is never moved on.

Settings: max_examples=30 runs of up to 40 steps (stateful_step_count),
at most 3 orders per run, no deadline. About 30-40 s on a laptop: each
step is one or a few HTTP requests against SQLite; 30 x 40 steps hit
every rule many times while keeping the suite's runtime reasonable (a
failure costs more: hypothesis then shrinks it to a minimal sequence).
This found a real bug while it was written: a mismatching success report
about an attempt that had already paid the order flagged that attempt
(see test_webhook_events.py).
"""
from collections import Counter
from datetime import timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck
from hypothesis import settings as hyp_settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    initialize,
    invariant,
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
    reset_provider,
)

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.models.order import Order
from app.models.payment_ledger import PaymentDispute, PaymentRefund
from app.models.product import Product
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import (
    CheckoutSessionStatus,
    PaymentIntentStatus,
)

# Answers seen during the run, by route and status code.
SEEN = Counter()

STOCK = 15
PRICE = "7.35"
MAX_ORDERS = 3


class PaymentChain(RuleBasedStateMachine):
    shop: Shop = None  # set by the test function

    @initialize()
    def setup(self):
        reset_provider()
        self.provider = StripePaymentProvider()
        self.product = self.shop.product(price=PRICE, stock=STOCK)
        self.buyer = self.shop.buyer()
        self.admin = self.shop.admin()
        self.orders = []

    # -- helpers ------------------------------------------------------------

    def _ok(self, response, *allowed):
        SEEN[f"{response.request.url.path.split('/')[-1]} {response.status_code}"] += 1
        assert response.status_code != 500, response.text
        assert response.status_code in allowed, (response.status_code, response.text)
        return response

    def _order_id(self, index):
        return self.orders[index % len(self.orders)]

    def _attempt(self, order_id, index, predicate):
        attempts = [p for p in self.shop.payments(order_id) if predicate(p)]
        return attempts[index % len(attempts)] if attempts else None

    def _mock_intent(self, payment_intent_id):
        return StripePaymentProvider._intents_by_id.get(payment_intent_id)

    # -- customers ------------------------------------------------------------

    @precondition(lambda self: len(self.orders) < MAX_ORDERS)
    @rule(quantity=st.integers(1, 6))
    def create_order(self, quantity):
        self._ok(self.shop.add_to_cart(self.buyer, self.product, quantity), 200, 409)
        response = self._ok(self.shop.place_order(self.buyer), 201, 400, 409)
        if response.status_code == 201:
            self.orders.append(response.json()["id"])
        self.shop.client.delete("/api/v1/cart", headers=self.buyer.headers)

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10), card=st.sampled_from([PAY_OK, PAY_DECLINED, PAY_3DS]))
    def pay(self, index, card):
        self._ok(self.shop.pay(self.buyer, self._order_id(index), token=card),
                 200, 409, 422)

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10))
    def pay_with_a_good_card(self, index):
        # The common case, as a rule of its own so that it is drawn as
        # often as all the card variants together.
        self._ok(self.shop.pay(self.buyer, self._order_id(index)), 200, 409)

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10))
    def open_checkout_session(self, index):
        self._ok(self.shop.checkout_session(self.buyer, self._order_id(index)), 200, 409)

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10))
    def customer_cancels(self, index):
        self._ok(self.shop.cancel(self.buyer, self._order_id(index)), 200, 409)

    # -- Stripe --------------------------------------------------------------------

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10), attempt=st.integers(0, 5),
          verdict=st.sampled_from(["succeeded", "succeeded_wrong_amount",
                                   "payment_failed", "canceled"]))
    def intent_verdict(self, index, attempt, verdict):
        payment = self._attempt(
            self._order_id(index), attempt,
            lambda p: p.checkout_session_id is None and not p.payment_intent_id.startswith("pending-"))
        if payment is None:
            return
        intent = self._mock_intent(payment.payment_intent_id)
        if verdict.startswith("succeeded"):
            if intent.status == PaymentIntentStatus.REQUIRES_ACTION:
                self.provider.confirm_payment_intent(intent.id, return_url="https://shop.test/r")
            if intent.status != PaymentIntentStatus.SUCCEEDED:
                return
            amount = payment.amount + (1 if verdict == "succeeded_wrong_amount" else 0)
            self._ok(self.shop.intent_event("payment_intent.succeeded", payment, amount=amount), 200)
        else:
            if intent.status == PaymentIntentStatus.SUCCEEDED:
                return  # Stripe cannot fail or cancel a succeeded intent
            if verdict == "canceled":
                intent.status = PaymentIntentStatus.CANCELED
            self._ok(self.shop.intent_event(f"payment_intent.{verdict}", payment), 200)

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10), attempt=st.integers(0, 5),
          outcome=st.sampled_from(["completed", "expired", "async_payment_failed"]))
    def session_outcome(self, index, attempt, outcome):
        payment = self._attempt(self._order_id(index), attempt,
                                lambda p: p.checkout_session_id is not None)
        if payment is None:
            return
        session = StripePaymentProvider._sessions_by_id[payment.checkout_session_id]
        if session.status != CheckoutSessionStatus.OPEN:
            return
        if outcome == "completed":
            # The customer pays on Stripe's page: a PaymentIntent that
            # succeeded, attached to the session.
            intent = self.provider.create_payment_intent(
                amount=session.amount_total, currency=session.currency,
                idempotency_key=f"cs-{session.id}", payment_method=PAY_OK)
            self.provider.confirm_payment_intent(intent.id, return_url="https://shop.test/r")
            session.status = CheckoutSessionStatus.COMPLETE
            session.payment_intent = intent.id
            self._ok(self.shop.session_event("checkout.session.completed", payment,
                                             payment_intent=intent.id), 200)
        else:
            session.status = CheckoutSessionStatus.EXPIRED
            self._ok(self.shop.session_event(f"checkout.session.{outcome}", payment,
                                             payment_status="unpaid"), 200)

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10), amount=st.integers(1, 3000))
    def dashboard_refund(self, index, amount):
        payment = self._attempt(self._order_id(index), 0, lambda p: p.status == "succeeded")
        intent = payment and self._mock_intent(payment.payment_intent_id)
        if intent is None or intent.status != PaymentIntentStatus.SUCCEEDED:
            return
        if amount > intent.amount - intent.amount_refunded:
            return  # Stripe refuses it
        refund = self.provider.create_refund(payment_intent_id=intent.id, amount=amount)
        self._ok(self.shop.webhook("refund.created", {
            "id": refund.id, "object": "refund", "amount": refund.amount,
            "currency": payment.currency.lower(), "payment_intent": intent.id,
            "status": "succeeded"}), 200)

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10), which=st.integers(0, 5))
    def refund_fails_later(self, index, which):
        order_id = self._order_id(index)
        self.shop.db.expire_all()
        refunds = (self.shop.db.query(PaymentRefund)
                   .join(PaymentRefund.payment).filter_by(order_id=order_id)
                   .filter(PaymentRefund.status == "succeeded").all())
        if not refunds:
            return
        refund = refunds[which % len(refunds)]
        intent = self._mock_intent(refund.payment.payment_intent_id)
        intent.amount_refunded -= refund.amount  # Stripe returns it to the charge
        self._ok(self.shop.webhook("refund.failed", {
            "id": refund.refund_id, "object": "refund", "amount": refund.amount,
            "currency": refund.currency.lower(), "payment_intent": intent.id,
            "status": "failed", "failure_reason": "expired_or_canceled_card"}), 200)

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10),
          status=st.sampled_from(["needs_response", "under_review", "won", "lost",
                                  "warning_closed"]))
    def dispute(self, index, status):
        payment = self._attempt(self._order_id(index), 0, lambda p: p.status == "succeeded")
        if payment is None:
            return
        event_type = ("charge.dispute.closed"
                      if status in ("won", "lost", "warning_closed")
                      else "charge.dispute.created")
        self._ok(self.shop.webhook(event_type, {
            "id": f"du_{payment.id}", "object": "dispute", "amount": payment.amount,
            "currency": payment.currency.lower(), "payment_intent": payment.payment_intent_id,
            "status": status, "reason": "fraudulent"}), 200)

    # -- admins and the expiry task ---------------------------------------------------

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10), amount=st.one_of(st.none(), st.integers(1, 3000)))
    def admin_refunds(self, index, amount):
        value = None if amount is None else Decimal(amount) / 100
        response = self._ok(
            self.shop.refund(self.admin, self._order_id(index), amount=value), 200, 400, 409)
        if response.status_code == 400:
            # The provider refusing more than is left on the charge. The
            # route does not check the remainder itself: a replay of a
            # refund that took everything that was left (same
            # Idempotency-Key) must answer the first refund, and only the
            # provider knows the key. Any other 400 would be a bug.
            assert "exceeds the refundable amount" in response.json()["detail"]

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10),
          status=st.sampled_from(["confirmed", "processing", "sent", "delivered", "cancelled"]))
    def admin_sets_status(self, index, status):
        order_id = self._order_id(index)
        before = self.shop.order(order_id).status
        response = self._ok(self.shop.set_status(self.admin, order_id, status), 200, 409)
        if response.status_code == 409:
            assert self.shop.order(order_id).status == before
            if response.json()["detail"] != "Invalid status transition":
                assert self._open_dispute(order_id) and status != "cancelled"

    @precondition(lambda self: self.orders)
    @rule(index=st.integers(0, 10))
    def order_outlives_the_unpaid_window(self, index):
        # This order is old enough to expire if unpaid; the task runs.
        self.shop.db.get(Order, self._order_id(index)).created_at = (
            utc_now() - timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES + 1))
        self.shop.db.commit()
        tasks.cancel_expired_unpaid_orders()

    # -- invariants --------------------------------------------------------------------

    def _open_dispute(self, order_id):
        return any(d.status not in ("won", "lost", "warning_closed", "prevented")
                   for p in self.shop.payments(order_id) for d in p.disputes)

    @invariant()
    def money_and_stock_agree(self):
        if not getattr(self, "orders", None):
            return
        db = self.shop.db
        db.expire_all()
        live_quantity = 0
        for order_id in self.orders:
            order = db.get(Order, order_id)
            payments = self.shop.payments(order_id)
            applied = [p for p in payments if p.status == "succeeded" and not p.requires_manual_review]
            assert len(applied) <= 1, [(p.id, p.status) for p in payments]
            if order.payment_status in ("paid", "refunded"):
                assert len(applied) == 1, (order.payment_status, [(p.status, p.requires_manual_review) for p in payments])
                charge = applied[0]
                assert charge.amount == cents(order.total_amount)
                assert charge.currency == order.currency
                refunds = db.query(PaymentRefund).filter_by(payment_id=charge.id).all()
                returned = sum(r.amount for r in refunds if r.status == "succeeded")
                lost = db.query(PaymentDispute).filter_by(payment_id=charge.id, status="lost").first()
                if order.payment_status == "refunded":
                    assert lost is not None or returned >= charge.amount
                else:
                    assert lost is None and returned < charge.amount
            else:
                assert applied == []
            for payment in payments:
                assert 0 <= payment.amount_refunded <= payment.amount
                refunds = db.query(PaymentRefund).filter_by(payment_id=payment.id).all()
                assert payment.amount_refunded == sum(
                    r.amount for r in refunds if r.status not in ("failed", "canceled"))
                if payment.status != "succeeded":
                    assert payment.amount_refunded == 0
                intent = self._mock_intent(payment.payment_intent_id)
                if intent is not None:
                    assert intent.amount_refunded == payment.amount_refunded
            if order.status != "cancelled":
                live_quantity += sum(item.quantity for item in order.items)
        assert db.get(Product, self.product.id).stock_quantity == STOCK - live_quantity


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    for name in ("send_order_confirmation_email", "send_order_status_update_email",
                 "send_order_cancellation_email"):
        monkeypatch.setattr(f"app.api.v1.orders.{name}", lambda *a, **k: True)
    monkeypatch.setattr("app.tasks.send_order_cancellation_email", lambda *a, **k: True)
    yield
    reset_provider()


def test_the_payment_chain_keeps_its_invariants(client, db_session, tasks_db, monkeypatch):
    PaymentChain.shop = Shop(client, db_session)
    SEEN.clear()
    try:
        run_state_machine_as_test(PaymentChain, settings=hyp_settings(
            max_examples=30,
            stateful_step_count=40,
            deadline=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
        ))
    finally:
        PaymentChain.shop = None
    # A floor, not a target: the run did place, pay and report on orders
    # (refusals alone would satisfy the invariants trivially). Rarer
    # outcomes vary from run to run and are not required.
    for outcome in ("orders 201", "pay 200", "stripe 200"):
        assert SEEN[outcome] > 0, (outcome, SEEN)
