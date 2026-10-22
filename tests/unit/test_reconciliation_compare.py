"""
reconciliation.compare(): one payment's record against the provider's, as a
pure function. Which of our statuses agree with which PaymentIntent status,
and the refund comparison by itself.
"""
import pytest

from app.models.payment_discrepancy import (
    KIND_AMOUNT,
    KIND_PAYMENT_MISSING,
    KIND_REFUND_NOT_RECORDED,
    KIND_REFUND_STATUS,
    KIND_REFUNDED_TOTAL,
    KIND_STATUS,
)
from app.services.payment.types import PaymentIntent, PaymentIntentStatus, Refund, RefundStatus
from app.services.reconciliation import (
    Difference,
    PaymentSnapshot,
    RefundSnapshot,
    compare,
    provider_refunded_total,
)

S = PaymentIntentStatus


def snapshot(status="succeeded", amount=5000, refunded=0, refunds=()):
    return PaymentSnapshot(1, "pi_1", status, amount, refunded, "USD", tuple(refunds))


def intent(status=S.SUCCEEDED, amount=5000, received=None, currency="USD"):
    if received is None:
        received = amount if status == S.SUCCEEDED else 0
    return PaymentIntent("pi_1", amount, currency, status, "secret", amount_received=received)


AGREEING = [
    ("succeeded", S.SUCCEEDED),
    ("canceled", S.CANCELED),
    ("failed", S.REQUIRES_PAYMENT_METHOD),
    ("failed", S.REQUIRES_CONFIRMATION),
    ("failed", S.REQUIRES_ACTION),
    ("failed", S.CANCELED),
    ("processing", S.PROCESSING),
    ("processing", S.REQUIRES_CONFIRMATION),
    ("requires_action", S.REQUIRES_ACTION),
]


@pytest.mark.parametrize("ours,theirs", AGREEING)
def test_statuses_that_agree(ours, theirs):
    assert compare(snapshot(ours), intent(theirs), []) == []


DISAGREEING = [
    ("succeeded", S.PROCESSING), ("succeeded", S.CANCELED),
    ("succeeded", S.REQUIRES_PAYMENT_METHOD),
    ("failed", S.SUCCEEDED), ("failed", S.PROCESSING),
    ("canceled", S.SUCCEEDED), ("canceled", S.REQUIRES_PAYMENT_METHOD),
    ("processing", S.SUCCEEDED), ("processing", S.CANCELED),
    ("processing", S.REQUIRES_PAYMENT_METHOD),
    ("requires_action", S.SUCCEEDED), ("requires_action", S.PROCESSING),
    ("a status nobody wrote", S.SUCCEEDED),
]


@pytest.mark.parametrize("ours,theirs", DISAGREEING)
def test_statuses_that_differ(ours, theirs):
    found = compare(snapshot(ours), intent(theirs), [])

    assert Difference(KIND_STATUS, "", ours, theirs.value) in found


def test_a_payment_the_provider_does_not_have_is_one_difference():
    assert compare(snapshot(), None, []) == [Difference(KIND_PAYMENT_MISSING, "", "pi_1", None)]


def test_a_succeeded_payment_is_compared_on_the_amount_received_not_the_amount_asked():
    found = compare(snapshot(amount=5000), intent(S.SUCCEEDED, amount=5000, received=4000), [])

    assert found == [Difference(KIND_AMOUNT, "", "5000", "4000")]


def test_currencies_are_compared_without_regard_to_case():
    assert compare(snapshot(), intent(currency="usd"), []) == []


def refund(refund_id="re_1", amount=1000, status=RefundStatus.SUCCEEDED, provider_status=None):
    return Refund(refund_id, "pi_1", amount, "USD", status, provider_status)


@pytest.mark.parametrize("refunds,total", [
    ([], 0),
    ([refund()], 1000),
    ([refund(), refund("re_2", 500)], 1500),
    ([refund(status=RefundStatus.PENDING, provider_status="pending")], 1000),
    ([refund(status=RefundStatus.FAILED, provider_status="failed")], 0),
    ([refund(status=RefundStatus.PENDING, provider_status="canceled")], 0),
    ([refund(status=RefundStatus.FAILED)], 0),
])
def test_what_the_provider_has_refunded(refunds, total):
    assert provider_refunded_total(refunds) == total


def test_a_refund_without_provider_status_is_read_by_its_status():
    found = compare(
        snapshot(refunded=1000, refunds=[RefundSnapshot("re_1", 1000, "pending")]),
        intent(), [refund()])

    assert found == [Difference(KIND_REFUND_STATUS, "re_1", "pending", "succeeded")]


def test_a_canceled_refund_is_not_part_of_the_total_but_is_a_status_difference():
    ours = RefundSnapshot("re_1", 1000, "pending")
    found = compare(
        snapshot(refunded=1000, refunds=[ours]),
        intent(), [refund(status=RefundStatus.PENDING, provider_status="canceled")])

    assert found == [
        Difference(KIND_REFUNDED_TOTAL, "", "1000", "0"),
        Difference(KIND_REFUND_STATUS, "re_1", "pending", "canceled"),
    ]


def test_a_refund_only_the_provider_has_carries_its_status_and_amount():
    found = compare(snapshot(), intent(), [refund("re_9", 250)])

    assert Difference(KIND_REFUND_NOT_RECORDED, "re_9", None, "succeeded 250") in found
