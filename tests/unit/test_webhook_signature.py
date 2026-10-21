"""
Webhook signature verification, the same assertions against both
providers: the mock (its own HMAC check) and the live one (which hands the
check to the real stripe SDK, stripe.Webhook.construct_event - nothing is
stubbed here). A webhook is the one request this application accepts from
anyone without a login, so the two must agree on every header and body.

The signatures are computed here, independently of the mock's own
sign_payload, the way Stripe documents it: HMAC-SHA256 with the endpoint's
secret over "{timestamp}.{body}", sent as "t={timestamp},v1={hex}"
(https://docs.stripe.com/webhooks#verify-manually). Times are epoch
seconds; every instant below is built from an aware UTC datetime, never
from the machine's local zone.
"""
import hashlib
import hmac
import json
from datetime import datetime, timezone

import pytest
from freezegun import freeze_time

from app.services.payment.exceptions import SignatureVerificationError
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.payment.stripe_provider import StripePaymentProvider

SECRET = "webhook-signing-secret-for-tests-not-a-real-one"
OTHER_SECRET = "another-webhook-signing-secret-for-tests"
TOLERANCE = 300
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())  # aware: independent of the local zone


def _signature(body: bytes, secret: str, timestamp: int) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def _header(body: bytes, secret: str = SECRET, timestamp: int = None) -> str:
    timestamp = _epoch(NOW) if timestamp is None else timestamp
    return f"t={timestamp},v1={_signature(body, secret, timestamp)}"


def _body(**overrides) -> bytes:
    event = {"id": "evt_1", "object": "event", "type": "payment_intent.succeeded",
             "created": _epoch(NOW), "data": {"object": {"id": "pi_1", "amount": 1999}}}
    event.update(overrides)
    return json.dumps(event).encode()


@pytest.fixture(params=["mock", "live"])
def provider(request):
    if request.param == "mock":
        return StripePaymentProvider()
    return LiveStripePaymentProvider(api_key="stripe-secret-key-for-tests-not-a-real-key")


@pytest.fixture(autouse=True)
def _now():
    with freeze_time(NOW):
        yield


def _verify(provider, body, header, secret=SECRET, tolerance=TOLERANCE):
    return provider.construct_webhook_event(body, header, secret, tolerance_seconds=tolerance)


def _refused(provider, body, header, secret=SECRET, tolerance=TOLERANCE):
    with pytest.raises(SignatureVerificationError):
        _verify(provider, body, header, secret, tolerance)


# -- accepted ---------------------------------------------------------------------

def test_a_correctly_signed_event_is_returned(provider):
    event = _verify(provider, _body(), _header(_body()))

    assert (event.id, event.type, event.created) == (
        "evt_1", "payment_intent.succeeded", _epoch(NOW)
    )
    assert event.data == {"object": {"id": "pi_1", "amount": 1999}}


def test_the_mocks_signing_helper_signs_as_stripe_does(provider):
    body = _body()
    header = StripePaymentProvider.sign_payload(body, SECRET, timestamp=_epoch(NOW))
    assert header == _header(body)
    assert _verify(provider, body, header).id == "evt_1"


# -- tampering and the wrong key --------------------------------------------------

def test_a_body_changed_after_signing_is_refused(provider):
    _refused(provider, _body(type="payment_intent.payment_failed"), _header(_body()))


def test_one_flipped_byte_is_refused(provider):
    body = _body()
    tampered = body.replace(b"1999", b"1998")
    _refused(provider, tampered, _header(body))


def test_a_signature_made_with_another_secret_is_refused(provider):
    _refused(provider, _body(), _header(_body(), secret=OTHER_SECRET))


def test_a_signature_of_another_timestamp_is_refused(provider):
    body = _body()
    signature = _signature(body, SECRET, _epoch(NOW) - 1)
    _refused(provider, body, f"t={_epoch(NOW)},v1={signature}")


def test_an_empty_secret_verifies_nothing(provider):
    # With no secret configured, a body "signed" with an empty key must not
    # pass: Stripe's library refuses to verify anything without a secret.
    body = _body()
    _refused(provider, body, _header(body, secret=""), secret="")


# -- the header ---------------------------------------------------------------------

@pytest.mark.parametrize("header", [None, ""])
def test_a_missing_header_is_refused(provider, header):
    _refused(provider, _body(), header)


def _garbled_headers():
    body = _body()
    ts = _epoch(NOW)
    sig = _signature(body, SECRET, ts)
    return {
        "no structure": "not-a-signature",
        "commas only": ",,,",
        "no timestamp": f"v1={sig}",
        "empty timestamp": f"t=,v1={sig}",
        "timestamp not a number": f"t=noon,v1={sig}",
        "no signature": f"t={ts}",
        "empty signature": f"t={ts},v1=",
        "only the v0 scheme": f"t={ts},v0={sig}",
        "upper-case hex": f"t={ts},v1={sig.upper()}",
        "truncated signature": f"t={ts},v1={sig[:-1]}",
        # Stripe's library splits on "," and "=" and nothing else; a header
        # with spaces in it does not come from Stripe.
        "space after the comma": f"t={ts}, v1={sig}",
        "space around the equals sign": f"t = {ts},v1 = {sig}",
    }


@pytest.mark.parametrize("name", list(_garbled_headers()))
def test_a_garbled_header_is_refused(provider, name):
    _refused(provider, _body(), _garbled_headers()[name])


# Starlette decodes header bytes as latin-1, so anyone can send these. Both
# providers compared the signature with hmac.compare_digest, which raises
# TypeError for text that is not ASCII: the webhook answered 500, not 400.
@pytest.mark.parametrize("header", ["t={ts},v1=é", "t={ts},v1=" + "é" * 64,
                                    "t={ts},v1={sig}ÿ", "t=é,v1=00"],
                         ids=["one character", "signature-sized", "after a valid signature",
                              "in the timestamp"])
def test_a_header_with_characters_outside_ascii_is_refused(provider, header):
    body = _body()
    ts = _epoch(NOW)
    _refused(provider, body, header.format(ts=ts, sig=_signature(body, SECRET, ts)))


# While a webhook secret is being rolled, Stripe signs each event with the
# old and the new secret and sends both v1 signatures; any one of them
# matching is enough (stripe.WebhookSignature.verify_header).
@pytest.mark.parametrize("order", ["matching first", "matching last"])
def test_one_matching_signature_among_several_is_enough(provider, order):
    body = _body()
    ts = _epoch(NOW)
    good, other = _signature(body, SECRET, ts), _signature(body, OTHER_SECRET, ts)
    signatures = [good, other] if order == "matching first" else [other, good]
    header = f"t={ts}," + ",".join(f"v1={s}" for s in signatures)

    assert _verify(provider, body, header).id == "evt_1"


def test_several_signatures_none_matching_are_refused(provider):
    body = _body()
    ts = _epoch(NOW)
    header = f"t={ts},v1={_signature(body, OTHER_SECRET, ts)},v1={'0' * 64}"
    _refused(provider, body, header)


def test_a_v0_signature_next_to_a_valid_v1_is_ignored(provider):
    body = _body()
    ts = _epoch(NOW)
    header = f"t={ts},v1={_signature(body, SECRET, ts)},v0={'0' * 64}"
    assert _verify(provider, body, header).id == "evt_1"


# -- the timestamp ------------------------------------------------------------------

@pytest.mark.parametrize("age", [0, 1, TOLERANCE - 1, TOLERANCE])
def test_a_signature_within_the_tolerance_is_accepted(provider, age):
    ts = _epoch(NOW) - age
    assert _verify(provider, _body(), _header(_body(), timestamp=ts)).id == "evt_1"


@pytest.mark.parametrize("age", [TOLERANCE + 1, 3600, 86400 * 365])
def test_a_signature_older_than_the_tolerance_is_refused(provider, age):
    _refused(provider, _body(), _header(_body(), timestamp=_epoch(NOW) - age))


@pytest.mark.parametrize("ahead", [1, TOLERANCE])
def test_a_signature_a_little_in_the_future_is_accepted(provider, ahead):
    # Clock skew between Stripe and this server, within the same window.
    ts = _epoch(NOW) + ahead
    assert _verify(provider, _body(), _header(_body(), timestamp=ts)).id == "evt_1"
