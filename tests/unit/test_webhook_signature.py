"""
Webhook signature verification, the same assertions against both
providers: the mock (its own HMAC check) and the live one (which hands the
check to the real stripe SDK, stripe.WebhookSignature.verify_header - nothing is
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
from datetime import UTC, datetime

import pytest
from freezegun import freeze_time

from app.services.payment.exceptions import SignatureVerificationError
from app.services.payment.live_stripe_provider import LiveStripePaymentProvider
from app.services.payment.stripe_provider import StripePaymentProvider

SECRET = "webhook-signing-secret-for-tests-not-a-real-one"
OTHER_SECRET = "another-webhook-signing-secret-for-tests"
TOLERANCE = 300
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=UTC)


def _epoch(moment: datetime) -> int:
    return int(moment.timestamp())  # aware: independent of the local zone


def _signature(body: bytes, secret: str, timestamp: int) -> str:
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


def _header(body: bytes, secret: str = SECRET, timestamp: int | None = None) -> str:
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


# -- the body -------------------------------------------------------------------------

def test_a_body_that_is_not_utf8_is_refused_even_unsigned(provider):
    # Anyone can post this: with a made-up signature it must be a refusal
    # (400), not an exception the route does not expect (500).
    body = b"\xff\xfe{not utf-8"
    _refused(provider, body, f"t={_epoch(NOW)},v1={'0' * 64}")


def test_a_correctly_signed_body_that_is_not_utf8_is_refused(provider):
    body = b"\xff\xfe{not utf-8"
    _refused(provider, body, _header(body))


def test_a_correctly_signed_body_that_is_not_json_is_refused(provider):
    body = b"this is not json"
    _refused(provider, body, _header(body))


@pytest.mark.parametrize("body", [b"[1, 2]", b'"text"', b"null", b"42"])
def test_a_correctly_signed_body_that_is_not_a_json_object_is_refused(provider, body):
    _refused(provider, body, _header(body))


def test_an_event_missing_optional_fields_gets_the_same_defaults(provider):
    # Stripe always sends id, type, created and data; both providers fill
    # the same defaults rather than one failing where the other does not.
    body = json.dumps({"object": "event"}).encode()
    event = _verify(provider, body, _header(body))
    assert event.id.startswith("evt_")
    assert (event.type, event.data, event.created) == ("", {}, _epoch(NOW))


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


@pytest.mark.parametrize("ahead", [TOLERANCE + 1, 3600, 86400 * 365 * 10])
def test_a_signature_from_further_in_the_future_is_refused(provider, ahead):
    # A signature dated ahead would otherwise stay valid until that date
    # plus the tolerance: one captured delivery could be replayed for years.
    _refused(provider, _body(), _header(_body(), timestamp=_epoch(NOW) + ahead))


def test_a_zero_tolerance_accepts_only_the_current_second(provider):
    # Stripe's library treats a tolerance of 0 as "do not check the
    # timestamp at all"; both providers treat it as a window of zero
    # seconds instead, so a misconfigured 0 refuses webhooks (visible)
    # rather than accepting replays forever (silent).
    body = _body()
    assert _verify(provider, body, _header(body), tolerance=0).id == "evt_1"
    _refused(provider, body, _header(body, timestamp=_epoch(NOW) - 1), tolerance=0)
    _refused(provider, body, _header(body, timestamp=_epoch(NOW) - 86400), tolerance=0)


# -- time zones -----------------------------------------------------------------------
#
# The check compares epoch seconds (time.time()) with the header's epoch
# timestamp, so it cannot depend on the local zone or on a calendar
# boundary. Checked at instants where a local-time computation would go
# wrong: midnight UTC, month, year and leap-day ends, and the daylight
# saving changes of Europe and the US - each with the frozen clock shifted
# to a range of local offsets (freezegun's tz_offset changes local time,
# not UTC).

AWKWARD_INSTANTS = {
    "midnight UTC": datetime(2026, 10, 22, 0, 0, 0, tzinfo=UTC),
    "end of a 31-day month": datetime(2026, 10, 31, 23, 59, 59, tzinfo=UTC),
    "new year": datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC),
    "end of February in a leap year": datetime(2028, 2, 29, 23, 59, 59, tzinfo=UTC),
    "EU summer time begins": datetime(2026, 3, 29, 1, 0, 0, tzinfo=UTC),
    "EU summer time ends": datetime(2026, 10, 25, 1, 0, 0, tzinfo=UTC),
    "US daylight time begins": datetime(2026, 3, 8, 7, 0, 0, tzinfo=UTC),
    "US daylight time ends": datetime(2026, 11, 1, 6, 0, 0, tzinfo=UTC),
}
LOCAL_OFFSETS = [-12, -5, 0, 1, 2, 5.5, 14]


@pytest.mark.parametrize("instant", list(AWKWARD_INSTANTS), ids=list(AWKWARD_INSTANTS))
@pytest.mark.parametrize("offset", LOCAL_OFFSETS)
def test_the_tolerance_window_is_the_same_at_any_instant_in_any_zone(provider, instant, offset):
    moment = AWKWARD_INSTANTS[instant]
    ts = _epoch(moment)
    body = _body(created=ts)
    with freeze_time(moment, tz_offset=offset):
        assert _verify(provider, body, _header(body, timestamp=ts)).created == ts
        assert _verify(provider, body, _header(body, timestamp=ts - TOLERANCE)).id == "evt_1"
        assert _verify(provider, body, _header(body, timestamp=ts + TOLERANCE)).id == "evt_1"
        _refused(provider, body, _header(body, timestamp=ts - TOLERANCE - 1))
        _refused(provider, body, _header(body, timestamp=ts + TOLERANCE + 1))
        # An hour off - what a local-time slip across a DST change would be.
        _refused(provider, body, _header(body, timestamp=ts - 3600))
        _refused(provider, body, _header(body, timestamp=ts + 3600))
