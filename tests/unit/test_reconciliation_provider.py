"""
The read operations the daily reconciliation needs from a PaymentProvider,
asserted once and run against both implementations (the mock and the live
adapter on top of an in-process stand-in for api.stripe.com, as in
test_payment_provider_contract.py):

- list_refunds(payment_intent_id): every refund of the PaymentIntent, each
  with the status exactly as the provider reports it (provider_status).
- PaymentIntent.amount_received: what was collected, 0 before the payment
  succeeds, and not reduced by refunds.

Then what only the live adapter does: read every page of a long list, refuse
to return a partial one, and translate the SDK's errors.

stripe.Refund.list(payment_intent=..., limit=..., starting_after=...) is the
call: the parameters are the ones in stripe-python 16's RefundListParams
(payment_intent, limit up to 100, starting_after) and the result a ListObject
with `data` and `has_more`.
"""
import pytest
import stripe
from fake_stripe_api import FakeStripeAPI, _Obj
from stripe import error as stripe_error

from app.services.payment import live_stripe_provider
from app.services.payment.exceptions import (
    InvalidRequestError,
    PaymentProviderTimeoutError,
)
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.payment.types import PaymentIntentStatus, RefundStatus

API_KEY = "stripe-secret-key-for-tests-not-a-real-key"
RETURN_URL = "http://localhost:3000/checkout/return?order_id=7"


class _Page:
    """A stripe ListObject: `data` and `has_more`."""

    def __init__(self, data, has_more):
        self.data = data
        self.has_more = has_more


class ListingFakeStripe(FakeStripeAPI):
    """The fake Stripe API with GET /v1/refunds, newest first, paged by
    limit and starting_after, filtered by payment_intent."""

    def install(self, monkeypatch):
        super().install(monkeypatch)
        monkeypatch.setattr(stripe.Refund, "list", self.refund_list)

    def refund_list(self, **params):
        _, params = self._request(params)
        assert set(params) <= {"payment_intent", "limit", "starting_after"}
        refunds = [r for r in reversed(list(self.refunds.values()))
                   if r["payment_intent"] == params["payment_intent"]]
        if "starting_after" in params:
            ids = [r["id"] for r in refunds]
            refunds = refunds[ids.index(params["starting_after"]) + 1:]
        limit = params.get("limit", 10)
        assert 1 <= limit <= 100
        return _Page([_Obj(r) for r in refunds[:limit]], len(refunds) > limit)


@pytest.fixture(params=["mock", "live"])
def stripe_side(request, monkeypatch):
    if request.param == "mock":
        StripePaymentProvider.reset_mock_state()
        provider = StripePaymentProvider()

        def go_offline():
            def unreachable(self):
                raise PaymentProviderTimeoutError()
            monkeypatch.setattr(StripePaymentProvider, "_simulate_network_call", unreachable)

        yield provider, StripePaymentProvider.settle_refund, go_offline
        StripePaymentProvider.reset_mock_state()
    else:
        fake = ListingFakeStripe(API_KEY)
        fake.install(monkeypatch)
        yield (LiveStripePaymentProvider(api_key=API_KEY), fake.settle_refund,
               lambda: setattr(fake, "offline", True))


_keys = iter(range(10_000))


def _intent(provider, token="pm_card_visa", amount=5000, currency="EUR", confirm=True):
    intent = provider.create_payment_intent(
        amount=amount, currency=currency, idempotency_key=f"recon-{next(_keys)}",
        payment_method=token)
    if confirm:
        intent = provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)
    return intent


# -- amount_received -----------------------------------------------------------------

def test_amount_received_is_zero_until_the_payment_succeeds(stripe_side):
    provider, _, _ = stripe_side
    created = _intent(provider, confirm=False)
    assert created.amount_received == 0
    paid = provider.confirm_payment_intent(created.id, return_url=RETURN_URL)
    assert (paid.status, paid.amount_received) == (PaymentIntentStatus.SUCCEEDED, 5000)
    assert provider.retrieve_payment_intent(created.id).amount_received == 5000


def test_amount_received_survives_a_refund(stripe_side):
    provider, _, _ = stripe_side
    paid = _intent(provider)
    provider.create_refund(payment_intent_id=paid.id, amount=1200)
    assert provider.retrieve_payment_intent(paid.id).amount_received == 5000


def test_amount_received_after_3d_secure(stripe_side):
    provider, _, _ = stripe_side
    intent = _intent(provider, "pm_card_authenticationRequired")
    assert intent.status == PaymentIntentStatus.REQUIRES_ACTION
    assert intent.amount_received == 0


def test_the_mock_reports_amount_received_after_3d_secure_completes():
    StripePaymentProvider.reset_mock_state()
    provider = StripePaymentProvider()
    intent = _intent(provider, "pm_card_authenticationRequired")
    done = provider.confirm_payment_intent(intent.id, return_url=RETURN_URL)
    assert (done.status, done.amount_received) == (PaymentIntentStatus.SUCCEEDED, 5000)
    StripePaymentProvider.reset_mock_state()


# -- list_refunds ------------------------------------------------------------------------

def test_an_intent_without_refunds_lists_none(stripe_side):
    provider, _, _ = stripe_side
    assert provider.list_refunds(_intent(provider).id) == []


def test_every_refund_of_the_intent_is_listed_and_no_other(stripe_side):
    provider, _, _ = stripe_side
    paid, other = _intent(provider, currency="PLN"), _intent(provider)
    first = provider.create_refund(payment_intent_id=paid.id, amount=1000)
    second = provider.create_refund(payment_intent_id=paid.id, amount=2500)
    provider.create_refund(payment_intent_id=other.id)

    listed = provider.list_refunds(paid.id)

    assert {r.id for r in listed} == {first.id, second.id}
    by_id = {r.id: r for r in listed}
    assert (by_id[first.id].amount, by_id[second.id].amount) == (1000, 2500)
    for refund in listed:
        assert (refund.payment_intent, refund.currency) == (paid.id, "PLN")
        assert (refund.status, refund.provider_status) == (RefundStatus.SUCCEEDED, "succeeded")


def test_a_pending_refund_is_listed_pending_and_then_succeeded(stripe_side):
    provider, settle, _ = stripe_side
    paid = _intent(provider, "pm_card_pendingRefund")
    refund = provider.create_refund(payment_intent_id=paid.id, amount=3000)

    (listed,) = provider.list_refunds(paid.id)
    assert (listed.status, listed.provider_status) == (RefundStatus.PENDING, "pending")

    settle(refund.id)
    (listed,) = provider.list_refunds(paid.id)
    assert (listed.status, listed.provider_status) == (RefundStatus.SUCCEEDED, "succeeded")


def test_a_refund_that_failed_later_is_listed_failed(stripe_side):
    provider, settle, _ = stripe_side
    paid = _intent(provider, "pm_card_refundFail")
    refund = provider.create_refund(payment_intent_id=paid.id)
    settle(refund.id)

    (listed,) = provider.list_refunds(paid.id)

    assert (listed.id, listed.status, listed.provider_status) == (
        refund.id, RefundStatus.FAILED, "failed")


def test_listing_refunds_reports_an_unreachable_stripe_as_a_timeout(stripe_side):
    provider, _, go_offline = stripe_side
    paid = _intent(provider)
    go_offline()
    with pytest.raises(PaymentProviderTimeoutError):
        provider.list_refunds(paid.id)


# -- the live adapter only ----------------------------------------------------------------

@pytest.fixture
def live(monkeypatch):
    fake = ListingFakeStripe(API_KEY)
    fake.install(monkeypatch)
    provider = LiveStripePaymentProvider(api_key=API_KEY)
    return provider, fake


def test_a_list_longer_than_a_page_is_read_page_by_page(live, monkeypatch):
    provider, fake = live
    monkeypatch.setattr(live_stripe_provider, "_REFUND_PAGE_SIZE", 2)
    paid = _intent(provider, amount=10_000)
    made = [provider.create_refund(payment_intent_id=paid.id, amount=100).id for _ in range(5)]

    listed = provider.list_refunds(paid.id)

    assert sorted(r.id for r in listed) == sorted(made)
    assert len(listed) == 5


def test_a_list_that_does_not_fit_the_page_cap_is_refused_not_cut_short(live, monkeypatch):
    provider, _ = live
    monkeypatch.setattr(live_stripe_provider, "_REFUND_PAGE_SIZE", 2)
    monkeypatch.setattr(live_stripe_provider, "_REFUND_MAX_PAGES", 2)
    paid = _intent(provider, amount=10_000)
    for _ in range(5):
        provider.create_refund(payment_intent_id=paid.id, amount=100)

    with pytest.raises(InvalidRequestError) as refused:
        provider.list_refunds(paid.id)
    assert refused.value.code == "too_many_refunds"


def test_the_page_size_is_what_stripe_allows_at_most(live):
    assert live_stripe_provider._REFUND_PAGE_SIZE == 100


def test_a_canceled_refund_keeps_its_status_in_provider_status(live):
    provider, fake = live
    paid = _intent(provider)
    refund = provider.create_refund(payment_intent_id=paid.id, amount=100)
    fake.refunds[refund.id]["status"] = "canceled"

    (listed,) = provider.list_refunds(paid.id)

    assert (listed.status, listed.provider_status) == (RefundStatus.PENDING, "canceled")


@pytest.mark.parametrize("raised,expected", [
    (stripe_error.RateLimitError("Too many requests"), PaymentProviderTimeoutError),
    (stripe_error.APIConnectionError("down"), PaymentProviderTimeoutError),
])
def test_list_refunds_translates_what_the_sdk_raises(live, monkeypatch, raised, expected):
    provider, _ = live

    def boom(**params):
        raise raised

    monkeypatch.setattr(stripe.Refund, "list", boom)
    with pytest.raises(expected):
        provider.list_refunds("pi_1")


def test_list_refunds_translates_a_refused_request(live, monkeypatch):
    provider, _ = live

    def boom(**params):
        raise stripe_error.InvalidRequestError("Bad id", "payment_intent", code="bad")

    monkeypatch.setattr(stripe.Refund, "list", boom)
    with pytest.raises(InvalidRequestError) as refused:
        provider.list_refunds("pi_1")
    assert (refused.value.param, refused.value.code) == ("payment_intent", "bad")


def test_retrieving_a_payment_intent_under_a_rate_limit_is_a_timeout(live, monkeypatch):
    provider, _ = live

    def boom(*args, **kwargs):
        raise stripe_error.RateLimitError("Too many requests")

    monkeypatch.setattr(stripe.PaymentIntent, "retrieve", boom)
    with pytest.raises(PaymentProviderTimeoutError):
        provider.retrieve_payment_intent("pi_1")
