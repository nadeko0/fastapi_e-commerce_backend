"""
The daily payment reconciliation (app/services/reconciliation.py), on the
in-process mock provider: every kind of difference between our payment
records and the provider's, no difference, idempotent re-runs, resolution, a
provider error for one payment, the window's boundaries, and the Celery task
and its beat entry.

Payments are made through the real HTTP API where that is how they come to
be (pay, refund) and then made to disagree with the provider the ways reality
makes them disagree: a webhook that never arrived, a refund made at the
provider, a row changed by hand.
"""
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from celery.schedules import crontab
from freezegun import freeze_time
from payment_support import PAY_OK, Shop, reset_provider

from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.models.payment import Payment
from app.models.payment_discrepancy import (
    KIND_AMOUNT,
    KIND_CURRENCY,
    KIND_PAYMENT_MISSING,
    KIND_REFUND_AMOUNT,
    KIND_REFUND_NOT_RECORDED,
    KIND_REFUND_STATUS,
    KIND_REFUND_UNKNOWN,
    KIND_REFUNDED_TOTAL,
    KIND_STATUS,
    PaymentDiscrepancy,
)
from app.models.payment_ledger import PaymentRefund
from app.services.payment.exceptions import CardError, PaymentProviderTimeoutError
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.reconciliation import reconcile_payments

PENDING_REFUND_CARD = "pm_card_pendingRefund"


@pytest.fixture(autouse=True)
def _clean_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session):
    return Shop(client, db_session)


@pytest.fixture
def provider():
    return StripePaymentProvider()


def _later():
    """A moment well after anything made in the test, past the minimum age."""
    return utc_now() + timedelta(hours=2)


def run(db, provider, **kwargs):
    kwargs.setdefault("now", _later())
    result = reconcile_payments(db, provider, **kwargs)
    db.expire_all()
    return result


def paid(shop, token=PAY_OK, price="25.00"):
    """A paid order and its payment."""
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product(price=price))
    response = shop.pay(buyer, order["id"], token=token)
    assert response.status_code == 200, response.text
    (payment,) = shop.payments(order["id"])
    assert payment.status == "succeeded"
    return order, payment


def rows(db, include_resolved=False):
    query = db.query(PaymentDiscrepancy).order_by(PaymentDiscrepancy.id)
    if not include_resolved:
        query = query.filter(PaymentDiscrepancy.resolved_at.is_(None))
    return query.all()


def found(db):
    """The open differences as (kind, subject, ours, theirs)."""
    return sorted((d.kind, d.subject, d.ours, d.theirs) for d in rows(db))


# -- no difference -----------------------------------------------------------------------

def test_a_payment_that_agrees_with_the_provider_is_not_reported(shop, provider):
    paid(shop)

    result = run(shop.db, provider)

    assert (result.checked, result.opened, result.errors) == (1, 0, 0)
    assert rows(shop.db, include_resolved=True) == []


def test_a_refunded_payment_that_agrees_is_not_reported(shop, provider):
    order, payment = paid(shop)
    admin = shop.admin()
    assert shop.refund(admin, order["id"], amount="10.00").status_code == 200
    assert shop.refund(admin, order["id"]).status_code == 200

    result = run(shop.db, provider)

    assert (result.checked, result.opened) == (1, 0)
    assert rows(shop.db, include_resolved=True) == []


def test_a_failed_and_a_declined_attempt_agree_with_a_waiting_intent(shop, provider):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    assert shop.pay(buyer, order["id"], token="pm_card_visa_chargeDeclined").status_code == 422

    result = run(shop.db, provider)

    assert (result.checked, result.opened) == (1, 0)


# -- each kind of difference -------------------------------------------------------------

def test_a_payment_the_provider_does_not_know(shop, provider):
    _, payment = paid(shop)
    reset_provider()

    run(shop.db, provider)

    assert found(shop.db) == [(KIND_PAYMENT_MISSING, "", payment.payment_intent_id, None)]


@pytest.mark.parametrize("ours", ["processing", "requires_action", "failed", "canceled"])
def test_a_succeeded_payment_we_never_heard_about(shop, provider, ours):
    # The lost webhook: the provider took the money, we still say otherwise.
    _, payment = paid(shop)
    payment.status = ours
    shop.db.commit()

    run(shop.db, provider)

    assert found(shop.db) == [(KIND_STATUS, "", ours, "succeeded")]


def test_a_success_we_recorded_that_the_provider_never_had(shop, provider):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product(price="25.00"))
    intent = provider.create_payment_intent(
        amount=2500, currency="USD", idempotency_key=uuid.uuid4().hex,
        payment_method=PAY_OK)
    payment = _direct_payment(shop.db, order["id"], intent.id, status="succeeded")

    run(shop.db, provider)

    assert (KIND_STATUS, "", "succeeded", "requires_confirmation") in found(shop.db)
    assert payment.id


def test_a_different_amount(shop, provider):
    _, payment = paid(shop, price="25.00")
    payment.amount = 2600
    shop.db.commit()

    run(shop.db, provider)

    assert found(shop.db) == [(KIND_AMOUNT, "", "2600", "2500")]


def test_a_different_currency(shop, provider):
    _, payment = paid(shop)
    payment.currency = "EUR"
    shop.db.commit()

    run(shop.db, provider)

    assert found(shop.db) == [(KIND_CURRENCY, "", "EUR", "USD")]


def test_the_amount_of_an_unpaid_intent_is_what_it_asks_for(shop, provider):
    # Not succeeded: there is no amount received yet to compare with.
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product(price="25.00"))
    intent = provider.create_payment_intent(
        amount=2500, currency="USD", idempotency_key=uuid.uuid4().hex)
    _direct_payment(shop.db, order["id"], intent.id, status="processing")

    run(shop.db, provider)

    assert found(shop.db) == []


def test_a_refund_made_at_the_provider_that_we_never_heard_about(shop, provider):
    # The Dashboard refund while the webhook endpoint was down.
    _, payment = paid(shop)
    refund = provider.create_refund(payment_intent_id=payment.payment_intent_id, amount=1000)

    run(shop.db, provider)

    assert found(shop.db) == [
        (KIND_REFUND_NOT_RECORDED, refund.id, None, "succeeded 1000"),
        (KIND_REFUNDED_TOTAL, "", "0", "1000"),
    ]


def test_a_refund_we_recorded_that_the_provider_does_not_have(shop, provider):
    _, payment = paid(shop)
    shop.db.add(PaymentRefund(
        payment_id=payment.id, refund_id="re_invented", amount=700, currency="USD",
        status="succeeded", source="admin"))
    payment.amount_refunded = 700
    shop.db.commit()

    run(shop.db, provider)

    assert found(shop.db) == [
        (KIND_REFUND_UNKNOWN, "re_invented", "succeeded 700", None),
        (KIND_REFUNDED_TOTAL, "", "700", "0"),
    ]


def test_a_refunded_total_that_does_not_add_up(shop, provider):
    order, payment = paid(shop)
    assert shop.refund(shop.admin(), order["id"], amount="10.00").status_code == 200
    payment.amount_refunded = 400
    shop.db.commit()

    run(shop.db, provider)

    assert found(shop.db) == [(KIND_REFUNDED_TOTAL, "", "400", "1000")]


def test_a_refund_whose_status_moved_on_without_us(shop, provider):
    order, payment = paid(shop, token=PENDING_REFUND_CARD)
    assert shop.refund(shop.admin(), order["id"], amount="10.00").status_code == 200
    (refund,) = payment.refunds
    assert refund.status == "pending"
    # Stripe finishes it; the refund.updated webhook is lost.
    StripePaymentProvider.settle_refund(refund.refund_id)

    run(shop.db, provider)

    assert found(shop.db) == [(KIND_REFUND_STATUS, refund.refund_id, "pending", "succeeded")]


def test_a_refund_that_failed_at_the_provider_leaves_the_total_to_the_others(shop, provider):
    order, payment = paid(shop, token="pm_card_refundFail")
    assert shop.refund(shop.admin(), order["id"]).status_code == 200
    (refund,) = payment.refunds
    StripePaymentProvider.settle_refund(refund.refund_id)  # refund.failed, lost

    run(shop.db, provider)

    # Their total is 0 (a failed refund returned nothing), ours still 2500.
    assert found(shop.db) == [
        (KIND_REFUND_STATUS, refund.refund_id, "succeeded", "failed"),
        (KIND_REFUNDED_TOTAL, "", "2500", "0"),
    ]


def test_a_refund_with_another_amount(shop, provider):
    order, payment = paid(shop)
    assert shop.refund(shop.admin(), order["id"], amount="10.00").status_code == 200
    (refund,) = payment.refunds
    refund.amount = 900
    payment.amount_refunded = 900
    shop.db.commit()

    run(shop.db, provider)

    assert found(shop.db) == [
        (KIND_REFUND_AMOUNT, refund.refund_id, "900", "1000"),
        (KIND_REFUNDED_TOTAL, "", "900", "1000"),
    ]


def test_several_differences_of_one_payment_are_all_reported(shop, provider):
    _, payment = paid(shop)
    payment.status = "processing"
    payment.amount = 2700
    payment.currency = "GBP"
    shop.db.commit()

    run(shop.db, provider)

    assert {d.kind for d in rows(shop.db)} == {KIND_STATUS, KIND_AMOUNT, KIND_CURRENCY}


def test_a_payment_is_not_flagged_for_manual_review_so_it_can_still_be_refunded(shop, provider):
    # requires_manual_review means "this charge did not pay the order" and
    # /refund passes such a payment over; a difference must not do that.
    order, payment = paid(shop)
    payment.status = "processing"
    shop.db.commit()
    run(shop.db, provider)
    assert rows(shop.db)

    assert shop.payments(order["id"])[0].requires_manual_review is False


# -- idempotence and resolution ----------------------------------------------------------

def test_running_twice_records_a_difference_once(shop, provider):
    _, payment = paid(shop)
    payment.amount = 2600
    shop.db.commit()
    first_run = utc_now() + timedelta(hours=2)

    first = run(shop.db, provider, now=first_run)
    second = run(shop.db, provider, now=first_run + timedelta(days=1))

    assert (first.opened, second.opened, second.resolved) == (1, 0, 0)
    (row,) = rows(shop.db, include_resolved=True)
    assert row.detected_at == first_run
    assert row.last_seen_at == first_run + timedelta(days=1)


def test_a_difference_that_changes_while_open_is_updated_not_duplicated(shop, provider):
    _, payment = paid(shop)
    payment.amount = 2600
    shop.db.commit()
    run(shop.db, provider)

    payment.amount = 2800
    shop.db.commit()
    result = run(shop.db, provider)

    assert result.opened == 0
    assert found(shop.db) == [(KIND_AMOUNT, "", "2800", "2500")]


def test_a_difference_that_goes_away_is_marked_resolved(shop, provider):
    _, payment = paid(shop)
    payment.amount = 2600
    shop.db.commit()
    run(shop.db, provider)
    resolved_at = utc_now() + timedelta(days=1)

    payment.amount = 2500
    shop.db.commit()
    result = run(shop.db, provider, now=resolved_at)

    assert (result.opened, result.resolved) == (0, 1)
    assert rows(shop.db) == []
    (row,) = rows(shop.db, include_resolved=True)
    assert row.resolved_at == resolved_at

    # Resolved stays resolved while it stays gone.
    again = run(shop.db, provider, now=resolved_at + timedelta(days=1))
    assert (again.opened, again.resolved) == (0, 0)
    (row,) = rows(shop.db, include_resolved=True)
    assert row.resolved_at == resolved_at


def test_a_difference_that_comes_back_reopens_its_row(shop, provider):
    _, payment = paid(shop)
    payment.amount = 2600
    shop.db.commit()
    run(shop.db, provider)
    payment.amount = 2500
    shop.db.commit()
    run(shop.db, provider)
    back_at = utc_now() + timedelta(days=3)

    payment.amount = 2600
    shop.db.commit()
    result = run(shop.db, provider, now=back_at)

    assert result.opened == 1
    (row,) = rows(shop.db, include_resolved=True)
    assert (row.resolved_at, row.detected_at) == (None, back_at)


# -- provider errors ---------------------------------------------------------------------

class FlakyProvider(StripePaymentProvider):
    """The mock, failing for the intents it is told to."""

    def __init__(self, fail_retrieve=(), fail_list=(), error=None):
        self.fail_retrieve = set(fail_retrieve)
        self.fail_list = set(fail_list)
        self.error = error or PaymentProviderTimeoutError()
        self.calls = []

    def retrieve_payment_intent(self, payment_intent_id):
        self.calls.append(payment_intent_id)
        if payment_intent_id in self.fail_retrieve:
            raise self.error
        return super().retrieve_payment_intent(payment_intent_id)

    def list_refunds(self, payment_intent_id):
        if payment_intent_id in self.fail_list:
            raise self.error
        return super().list_refunds(payment_intent_id)


def _three_payments(shop):
    return [paid(shop)[1] for _ in range(3)]


@pytest.mark.parametrize("where", ["retrieve", "list"])
def test_an_error_for_one_payment_does_not_stop_the_run(shop, where):
    first, second, third = _three_payments(shop)
    bad = {second.payment_intent_id}
    flaky = FlakyProvider(**{f"fail_{'retrieve' if where == 'retrieve' else 'list'}": bad})
    for payment in (first, second, third):
        payment.amount = 2600
    shop.db.commit()

    result = run(shop.db, flaky)

    assert (result.checked, result.errors, result.aborted) == (2, 1, False)
    assert result.error_payment_ids == [second.id]
    assert {d.payment_id for d in rows(shop.db)} == {first.id, third.id}


def test_an_error_does_not_resolve_what_was_open(shop, provider):
    _, payment = paid(shop)
    payment.amount = 2600
    shop.db.commit()
    run(shop.db, provider)

    result = run(shop.db, FlakyProvider(fail_retrieve={payment.payment_intent_id}))

    assert result.errors == 1
    assert len(rows(shop.db)) == 1


def test_a_card_error_other_than_missing_is_an_error_for_that_payment(shop):
    (payment, *_) = _three_payments(shop)
    flaky = FlakyProvider(fail_retrieve={payment.payment_intent_id},
                          error=CardError("Nope", code="api_error"))

    result = run(shop.db, flaky)

    assert (result.checked, result.errors) == (2, 1)


def test_the_run_stops_after_too_many_errors_in_a_row(shop, monkeypatch):
    monkeypatch.setattr(settings, "RECONCILIATION_MAX_CONSECUTIVE_ERRORS", 2)
    payments = _three_payments(shop)
    flaky = FlakyProvider(fail_retrieve={p.payment_intent_id for p in payments})

    result = run(shop.db, flaky)

    assert (result.checked, result.errors, result.aborted) == (0, 2, True)
    assert len(flaky.calls) == 2


def test_errors_that_are_not_in_a_row_do_not_stop_the_run(shop, monkeypatch):
    monkeypatch.setattr(settings, "RECONCILIATION_MAX_CONSECUTIVE_ERRORS", 2)
    first, second, third = _three_payments(shop)
    # Newest first: third, second, first. Fail third and first.
    flaky = FlakyProvider(fail_retrieve={third.payment_intent_id, first.payment_intent_id})

    result = run(shop.db, flaky)

    assert (result.checked, result.errors, result.aborted) == (1, 2, False)


# -- which payments ----------------------------------------------------------------------

def test_a_checkout_attempt_without_a_payment_intent_yet_is_skipped(shop, provider):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product())
    assert shop.checkout_session(buyer, order["id"]).status_code == 200
    (payment,) = shop.payments(order["id"])
    assert payment.payment_intent_id.startswith("pending-")

    result = run(shop.db, provider)

    assert (result.checked, result.skipped, result.errors) == (0, 1, 0)


def test_the_run_is_capped_newest_first(shop, provider, monkeypatch):
    monkeypatch.setattr(settings, "RECONCILIATION_MAX_PAYMENTS", 2)
    first, second, third = _three_payments(shop)
    for payment in (first, second, third):
        payment.amount = 2600
    shop.db.commit()

    result = run(shop.db, provider)

    assert (result.checked, result.truncated) == (2, True)
    assert {d.payment_id for d in rows(shop.db)} == {second.id, third.id}


def test_a_run_that_fits_the_cap_is_not_truncated(shop, provider, monkeypatch):
    monkeypatch.setattr(settings, "RECONCILIATION_MAX_PAYMENTS", 3)
    _three_payments(shop)

    assert run(shop.db, provider).truncated is False


def _direct_payment(db, order_id, intent_id, *, status="succeeded", amount=2500,
                    created_at=None):
    payment = Payment(
        order_id=order_id, provider="stripe", payment_intent_id=intent_id,
        amount=amount, currency="USD", status=status,
        **({"created_at": created_at} if created_at else {}))
    db.add(payment)
    db.commit()
    return payment


NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


def _payment_made_at(shop, provider, order_id, created_at):
    intent = provider.create_payment_intent(
        amount=2500, currency="USD", idempotency_key=uuid.uuid4().hex,
        payment_method=PAY_OK)
    provider.confirm_payment_intent(intent.id, return_url="http://localhost:3000/r")
    # Ours says processing, the provider succeeded: a difference when checked.
    return _direct_payment(shop.db, order_id, intent.id, status="processing",
                           created_at=created_at)


@freeze_time(NOW)
def test_the_window_runs_from_the_window_days_back_to_the_minimum_age(shop, provider):
    order = shop.order_of(shop.buyer(), shop.product())
    window = timedelta(days=settings.RECONCILIATION_WINDOW_DAYS)
    age = timedelta(minutes=settings.RECONCILIATION_MIN_AGE_MINUTES)
    inside = {
        "oldest moment": NOW - window,
        "youngest moment": NOW - age,
        "middle": NOW - timedelta(days=2),
    }
    outside = {
        "a second too old": NOW - window - timedelta(seconds=1),
        "a second too young": NOW - age + timedelta(seconds=1),
        "just created": NOW,
    }
    made = {name: _payment_made_at(shop, provider, order["id"], at).id
            for name, at in {**inside, **outside}.items()}

    result = reconcile_payments(shop.db, provider)

    assert result.checked == len(inside)
    assert {d.payment_id for d in rows(shop.db)} == {made[name] for name in inside}


def test_the_window_follows_the_settings(shop, provider, monkeypatch):
    monkeypatch.setattr(settings, "RECONCILIATION_WINDOW_DAYS", 1)
    monkeypatch.setattr(settings, "RECONCILIATION_MIN_AGE_MINUTES", 0)
    order = shop.order_of(shop.buyer(), shop.product())
    with freeze_time(NOW):
        inside = _payment_made_at(shop, provider, order["id"], NOW)
        _payment_made_at(shop, provider, order["id"], NOW - timedelta(days=1, seconds=1))

        result = reconcile_payments(shop.db, provider)

    assert result.checked == 1
    assert {d.payment_id for d in rows(shop.db)} == {inside.id}


def test_one_order_is_checked_whatever_its_age(shop, provider):
    order = shop.order_of(shop.buyer(), shop.product())
    other = shop.order_of(shop.buyer(), shop.product())
    young = _payment_made_at(shop, provider, order["id"], utc_now())
    old = _payment_made_at(shop, provider, order["id"], utc_now() - timedelta(days=90))
    _payment_made_at(shop, provider, other["id"], utc_now() - timedelta(days=2))

    result = reconcile_payments(shop.db, provider, order_id=order["id"])

    assert result.checked == 2
    assert {d.payment_id for d in rows(shop.db)} == {young.id, old.id}


def test_an_order_without_payments_is_a_run_of_nothing(shop, provider):
    order = shop.order_of(shop.buyer(), shop.product())

    result = reconcile_payments(shop.db, provider, order_id=order["id"])

    assert result.as_dict() == {
        "checked": 0, "skipped": 0, "errors": 0, "opened": 0, "resolved": 0,
        "truncated": False, "aborted": False}


# -- the task and its schedule -----------------------------------------------------------

def test_the_task_reconciles_with_the_configured_provider(shop, tasks_db):
    _, payment = paid(shop)
    payment.amount = 2600
    shop.db.commit()

    with freeze_time(_later()):
        outcome = tasks.check_payment_reconciliation()

    assert outcome["checked"] == 1 and outcome["opened"] == 1
    assert found(shop.db) == [(KIND_AMOUNT, "", "2600", "2500")]


def test_the_task_can_be_limited_to_one_order(shop, tasks_db):
    order, payment = paid(shop)
    _, other = paid(shop)
    payment.amount = other.amount = 2600
    shop.db.commit()

    outcome = tasks.check_payment_reconciliation(order_id=order["id"])

    assert outcome["checked"] == 1
    assert {d.payment_id for d in rows(shop.db)} == {payment.id}


def test_beat_runs_it_daily_at_a_quiet_hour():
    entry = tasks.celery.conf.beat_schedule["check-payment-reconciliation"]

    assert entry["task"] == "check_payment_reconciliation"
    assert entry["schedule"] == crontab(hour=3, minute=15)
    assert tasks.celery.tasks["check_payment_reconciliation"].queue == "stats"


def test_the_existing_beat_entries_are_unchanged():
    assert list(tasks.celery.conf.beat_schedule)[:4] == [
        "cleanup-inactive-accounts", "update-product-stats", "check-low-stock",
        "cancel-expired-unpaid-orders",
    ]


def test_newly_opened_discrepancy_records_last_seen_at(shop, provider):
    _, payment = paid(shop)
    payment.amount = 2600
    shop.db.commit()
    now = utc_now() + timedelta(hours=2)

    result = run(shop.db, provider, now=now)
    assert result.opened == 1
    (row,) = rows(shop.db)
    assert row.last_seen_at == now
    assert row.detected_at == now


def test_multiple_differences_reopening_increment_opened_count(shop, provider):
    _, payment = paid(shop)
    payment.amount = 2600
    payment.status = "processing"
    shop.db.commit()
    run(shop.db, provider)
    assert len(rows(shop.db)) == 2

    payment.amount = 2500
    payment.status = "succeeded"
    shop.db.commit()
    resolved_res = run(shop.db, provider)
    assert resolved_res.resolved == 2

    payment.amount = 2600
    payment.status = "processing"
    shop.db.commit()
    reopen_res = run(shop.db, provider)
    assert reopen_res.opened == 2


def test_reconcile_payments_query_limits_to_cap_plus_one(shop, provider, monkeypatch):
    monkeypatch.setattr(settings, "RECONCILIATION_MAX_PAYMENTS", 2)
    captured = []
    orig_scalars = shop.db.scalars

    def spy_scalars(statement, *args, **kwargs):
        captured.append(statement)
        return orig_scalars(statement, *args, **kwargs)

    monkeypatch.setattr(shop.db, "scalars", spy_scalars)
    run(shop.db, provider)

    assert len(captured) >= 1
    assert captured[0]._limit == 3


def test_pending_checkout_sessions_skipped_and_loop_continues(shop, provider):
    paid(shop)
    buyer = shop.buyer()
    order1 = shop.order_of(buyer, shop.product())
    order2 = shop.order_of(buyer, shop.product())
    assert shop.checkout_session(buyer, order1["id"]).status_code == 200
    assert shop.checkout_session(buyer, order2["id"]).status_code == 200

    result = run(shop.db, provider)
    assert result.skipped == 2
    assert result.checked == 1


def test_multiple_payments_opening_discrepancies_accumulate(shop, provider):
    _, p1 = paid(shop)
    _, p2 = paid(shop)
    p1.amount = 2600
    p2.amount = 2600
    shop.db.commit()

    result = run(shop.db, provider)
    assert result.checked >= 2
    assert result.opened == 2


def test_multiple_payments_resolving_discrepancies_accumulate(shop, provider):
    _, p1 = paid(shop)
    _, p2 = paid(shop)
    p1.amount = 2600
    p2.amount = 2600
    shop.db.commit()
    run(shop.db, provider)

    p1.amount = 2500
    p2.amount = 2500
    shop.db.commit()

    result = run(shop.db, provider)
    assert result.checked >= 2
    assert result.resolved == 2
