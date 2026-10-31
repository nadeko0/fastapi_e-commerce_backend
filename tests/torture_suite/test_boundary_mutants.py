import torture_support  # noqa: F401, I001  (must precede payment_support & co.)

# Boundary & mutation torture suite (phase 2d).
#
# Every conditional boundary of the security, rate-limit, cart, order-expiry,
# retention, reconciliation, webhook and idempotency code is driven at the
# exact edge: T - 1us, T, T + 1us for a time window (the clock frozen, the row
# timestamps written with microseconds so the database's own precision and
# string/timestamptz comparison is exercised), and limit - 1, limit, limit + 1
# for a count. Wherever a route exists the boundary is crossed through the
# real HTTP API.
#
# The value expected AT the boundary comes from the documented intent (the
# settings comments in app/core/config.py, docstrings, docs/, RFC 7519 for a
# JWT's exp: "on or after" exp the token is expired), not from the code. A
# test named test_bug_* asserts the documented behaviour where the code
# disagrees; see .cloud_torture/02d_boundary_mutants.md for each one.

import json
import math
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import freezegun.config
import jwt
import pytest
from fastapi.testclient import TestClient
from freezegun import freeze_time
from payment_support import PAY_OK, Shop, reset_provider
from sqlalchemy import event
from sqlalchemy.orm import Session

import app.services.stripe_events as stripe_events
from app import tasks
from app.core.clock import utc_now
from app.core.config import settings
from app.core.rate_limit import Rate, _wait, parse_rate, rate_limit_headers
from app.core.security import (
    _MAX_USER_ID,
    create_access_token,
    generate_email_verification_token,
    generate_password_reset_token,
    get_password_hash,
    user_id_from_subject,
)
from app.main import app
from app.models.address import Address
from app.models.category import Category
from app.models.order import Order
from app.models.payment import Payment, WebhookEvent
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.schemas.cart import CART_KEY_PREFIX, GUEST_CART_KEY_PREFIX, CartOwner
from app.schemas.common import INT32_MAX
from app.services import redis_codec
from app.services.payment.exceptions import PaymentProviderTimeoutError
from app.services.payment.stripe_provider import StripePaymentProvider
from app.services.reconciliation import reconcile_payments
from app.services.stripe_events import retry_delay_seconds

API = settings.API_V1_STR
USERS = f"{API}/users"
CART = f"{API}/cart"
ORDERS = f"{API}/orders"
UNLIMITED = "1000000/minute"
PASSWORD = "Str0ngPass1"


def bug(reason: str):
    """Strict xfail for a confirmed backend defect (an XPASS fails the run)."""
    return pytest.mark.xfail(strict=True, reason=f"BUG DISCOVERED: {reason}")


BUG_D1 = ("logout blacklists the access token for int(exp - now) seconds, which floors, so the entry "
          "lapses up to 1 s before the token expires (app/core/security.py:625)")
BUG_D2 = ("a client waiting exactly Retry-After is refused again: Redis keeps the counter alive AT "
          "its expiry instant, 1 ms past the ceiled Retry-After (app/core/rate_limit.py:219-221,251-253)")
BUG_D4 = ("data-retention status floors the age to whole days ((now - created_at).days <= period), "
          "so an account up to 23h59m past its period is 'within' (app/services/gdpr.py:180-181)")
BUG_D5 = ("order total beyond Numeric(10,2) is never checked: 500 on PostgreSQL, stored on SQLite "
          "(app/api/v1/orders.py:366-368)")

US = timedelta(microseconds=1)
MS = timedelta(milliseconds=1)
ZERO = timedelta(0)
# A whole second, well after the real clock (tokens made inside a freeze at
# this instant are never "issued in the future" for the frozen PyJWT check).
T0 = datetime(2030, 1, 15, 12, 0, 0, tzinfo=UTC)

# (offset from the boundary instant, id) - the three cases of every window.
EDGE = [(-US, "T-1us"), (ZERO, "T"), (US, "T+1us")]
EDGE_IDS = [e[1] for e in EDGE]
EDGE_OFFSETS = [e[0] for e in EDGE]


# --- fixtures & helpers -------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch):
    """freeze_time everywhere: TestClient serves requests in a worker thread,
    and freezegun answers the real time under a "threading" frame unless its
    ignore list is emptied (see tests/integration/time_support.py). Row
    timestamps left to their utc_now default are stamped at flush time from
    the frozen clock as well."""
    monkeypatch.setattr(freezegun.config.settings, "default_ignore_list", [])

    def _stamp(session, _context, _instances):
        for obj in session.new:
            if hasattr(obj, "created_at") and obj.created_at is None:
                obj.created_at = utc_now()
            if hasattr(obj, "updated_at") and obj.updated_at is None:
                obj.updated_at = utc_now()

    event.listen(Session, "before_flush", _stamp)
    yield freeze_time
    event.remove(Session, "before_flush", _stamp)


@pytest.fixture(autouse=True)
def _quiet_emails(monkeypatch):
    for name in ("send_order_confirmation_email", "send_order_status_update_email",
                 "send_order_cancellation_email"):
        monkeypatch.setattr(f"app.api.v1.orders.{name}", lambda *a, **k: True)
    monkeypatch.setattr("app.tasks.send_order_cancellation_email", lambda *a, **k: True)


@pytest.fixture(autouse=True)
def _fresh_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def limits(monkeypatch):
    """The limiter on, every limit out of the way; a test lowers its own."""
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    for name in ("DEFAULT", "SEARCH", "AUTH", "CHECKOUT", "GUEST_CART",
                 "LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS", "LOGIN_FAILURES_PER_EMAIL"):
        monkeypatch.setattr(settings, f"RATE_LIMIT_{name}", UNLIMITED)

    def lower(**values):
        for name, value in values.items():
            monkeypatch.setattr(settings, name, value)

    return lower


def make_user(db, *, email=None, active=True, verified=True, role="client", **extra) -> User:
    user = User(email=email or f"u-{uuid.uuid4().hex[:10]}@example.com",
                hashed_password=get_password_hash(PASSWORD), full_name="Test User",
                phone="+15551234567", role=role, is_active=active,
                is_email_verified=verified, gdpr_consent=True,
                privacy_policy_accepted=True, **extra)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def make_address(db, user) -> Address:
    address = Address(user_id=user.id, street="1 Main Street", city="Metropolis",
                      state="NY", postal_code="10001", country="US")
    db.add(address)
    db.commit()
    return address


def make_product(db, *, stock=10, price="10.00", active=True) -> Product:
    category = Category(name=f"cat-{uuid.uuid4().hex[:8]}", path=[], level=0)
    db.add(category)
    db.commit()
    product = Product(name=f"Widget {uuid.uuid4().hex[:6]}", description="A fine widget",
                      price=Decimal(price), stock_quantity=stock, category_id=category.id,
                      images=["https://img.test/p.png"], is_active=active)
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def make_order(db, user, address, *, created_at, status="new", payment_status="pending",
               key=None, total="5.00") -> Order:
    order = Order(user_id=user.id, status=status, payment_status=payment_status,
                  shipping_address_id=address.id, total_amount=Decimal(total),
                  idempotency_key=key, idempotency_fingerprint="f" * 64 if key else None,
                  created_at=created_at, updated_at=created_at)
    db.add(order)
    db.commit()
    return order


def bearer(token) -> dict:
    return {"Authorization": f"Bearer {token}"}


def auth(user_id) -> dict:
    return bearer(create_access_token(user_id))


def login(client, email, password=PASSWORD):
    return client.post(f"{USERS}/login", data={"username": email, "password": password})


def claims(token) -> dict:
    return jwt.decode(token, options={"verify_signature": False})


def at(epoch) -> datetime:
    return datetime.fromtimestamp(epoch, UTC)


def https_client(address="203.0.113.7") -> TestClient:
    """A fresh cookie jar on https (the guest cart cookie is Secure)."""
    return TestClient(app, base_url="https://testserver", client=(address, 50000))


# =====================================================================================
# A. Pure boundaries: the rate arithmetic, the token subject, the event backoff.
# =====================================================================================


@pytest.mark.parametrize("text,expected", [
    ("1/second", Rate(1, 1)),
    ("1/seconds", Rate(1, 1)),
    ("300/minute", Rate(300, 60)),
    ("10/15 minutes", Rate(10, 900)),
    ("10/15minutes", Rate(10, 900)),
    ("100/hour", Rate(100, 3600)),
    ("1/1 day", Rate(1, 86400)),
    ("2147483647/day", Rate(2147483647, 86400)),
])
def test_parse_rate_accepts_the_smallest_and_largest_shapes(text, expected):
    assert parse_rate(text) == expected


@pytest.mark.parametrize("text", [
    "0/minute",        # zero requests: below the pattern's [1-9]
    "10/0 minutes",    # a zero-length window
    "01/minute",       # a leading zero is not the canonical count
    "-1/minute",
    "1/week",
    "1/ minute",
    "",
])
def test_parse_rate_refuses_just_outside_the_pattern(text):
    with pytest.raises(ValueError):
        parse_rate(text)


RATE = Rate(5, 60)


@pytest.mark.parametrize("count,ttl_ms,expected", [
    (RATE.count - 1, 60000, 0),     # limit - 1: allowed
    (RATE.count, 60000, 0),         # limit: allowed ("300/minute" = 300 requests)
    (RATE.count + 1, 60000, 60),    # limit + 1: refused for the rest of the window
    (RATE.count + 1, 59001, 60),    # ceil to whole seconds
    (RATE.count + 1, 59000, 59),
    (RATE.count + 1, 58999, 59),
    (RATE.count + 1, 1001, 2),
    (RATE.count + 1, 1000, 1),
    (RATE.count + 1, 999, 1),
    (RATE.count + 1, 1, 1),
    (RATE.count + 1, 0, 1),         # never "Retry-After: 0" on a refusal
    (RATE.count + 1, -1, 1),
])
def test_wait_is_zero_up_to_the_limit_and_the_ceiled_rest_of_the_window_after(
    count, ttl_ms, expected
):
    assert _wait(count, ttl_ms, RATE) == expected


@pytest.mark.parametrize("count,remaining", [(1, 4), (4, 1), (5, 0), (6, 0), (100, 0)])
def test_ratelimit_remaining_reaches_zero_exactly_at_the_limit(count, remaining):
    headers = rate_limit_headers("default", RATE, count, 30000)
    assert headers["RateLimit"] == f'"default";r={remaining};t=30'
    assert headers["RateLimit-Policy"] == '"default";q=5;w=60'


@pytest.mark.parametrize("ttl_ms,reset", [(0, 0), (1, 1), (999, 1), (1000, 1), (1001, 2), (-2, 0)])
def test_ratelimit_reset_is_the_ceiled_window_rest(ttl_ms, reset):
    assert rate_limit_headers("x", RATE, 1, ttl_ms)["RateLimit"].endswith(f";t={reset}")


@pytest.mark.parametrize("subject,expected", [
    ("0", None),                       # below the smallest id
    ("1", 1),
    (str(_MAX_USER_ID - 1), _MAX_USER_ID - 1),
    (str(_MAX_USER_ID), _MAX_USER_ID),  # the largest INTEGER id
    (str(_MAX_USER_ID + 1), None),      # one past it
    ("9999999999", None),               # ten digits, the regex's longest
    ("10000000000", None),              # eleven digits
    ("01", None),
    (" 1", None),
    (1, None),
])
def test_user_id_from_subject_at_the_int32_edge(subject, expected):
    assert user_id_from_subject(subject) == expected


@pytest.mark.parametrize("attempts,expected", [
    (1, 30), (2, 60), (3, 120), (4, 240), (5, 480),
    (6, 900),   # 960 capped
    (7, 900), (11, 900),
])
def test_retry_delay_doubles_until_the_cap(monkeypatch, attempts, expected):
    monkeypatch.setattr(settings, "STRIPE_EVENT_RETRY_BASE_SECONDS", 30)
    monkeypatch.setattr(settings, "STRIPE_EVENT_RETRY_MAX_SECONDS", 900)
    assert retry_delay_seconds(attempts) == expected


@pytest.mark.parametrize("cap,attempts,expected", [(480, 4, 240), (480, 5, 480), (480, 6, 480),
                                                   (479, 5, 479), (481, 5, 480)])
def test_retry_delay_cap_is_inclusive(monkeypatch, cap, attempts, expected):
    monkeypatch.setattr(settings, "STRIPE_EVENT_RETRY_BASE_SECONDS", 30)
    monkeypatch.setattr(settings, "STRIPE_EVENT_RETRY_MAX_SECONDS", cap)
    assert retry_delay_seconds(attempts) == expected


# --- cart arithmetic (no route reaches these two directly) ----------------------------


def _cart_with(owner, lines):
    cart = redis_codec.empty_cart(owner)
    for product_id, quantity in lines:
        cart.add_item(product_id=product_id, quantity=quantity, price=Decimal("1.00"),
                      name="x", image="")
    return cart


@pytest.mark.parametrize("in_cart,ordered,left", [(5, 4, 1), (5, 5, None), (5, 6, None),
                                                  (1, 1, None)])
def test_ordering_a_line_removes_it_exactly_when_all_of_it_was_ordered(in_cart, ordered, left):
    owner = CartOwner.account(1)
    stored = _cart_with(owner, [(7, in_cart)])
    write = redis_codec.plan_remove_ordered(owner, stored.model_dump_json(),
                                            _cart_with(owner, [(7, ordered)]))
    if left is None:
        assert write.cart is None
    else:
        assert write.cart.items["7"].quantity == left


@pytest.mark.parametrize("mine,theirs,max_lines,expected_lines,left_out", [
    (0, 3, 3, 3, 0),
    (0, 4, 3, 3, 1),
    (2, 1, 3, 3, 0),
    (2, 2, 3, 3, 1),
    (3, 1, 3, 3, 1),
    (3, 0, 3, 3, 0),
    (4, 1, 3, 4, 1),    # already above the cap: kept, nothing new taken
])
def test_cart_merge_takes_new_lines_while_below_the_cap(mine, theirs, max_lines,
                                                        expected_lines, left_out):
    account = _cart_with(CartOwner.account(1), [(i, 1) for i in range(1, mine + 1)])
    guest = _cart_with(CartOwner.guest(), [(100 + i, 1) for i in range(theirs)])
    assert account.merge(guest, max_lines) == left_out
    assert len(account.items) == expected_lines


# =====================================================================================
# B. Tokens: exp per RFC 7519 4.1.4 ("on or after" exp the JWT MUST NOT be
#    accepted), refresh, reset and verification links, logout, the watermark.
# =====================================================================================


@pytest.mark.parametrize("offset", EDGE_OFFSETS, ids=EDGE_IDS)
def test_access_token_is_refused_on_and_after_its_exp(client, db_session, clock, offset):
    user = make_user(db_session)
    with clock(T0) as frozen:
        response = login(client, user.email)
        assert response.status_code == 200, response.text
        token = response.json()["access_token"]
        exp = at(claims(token)["exp"])
        assert exp == T0 + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
        frozen.move_to(exp + offset)
        me = client.get(f"{USERS}/me", headers=bearer(token))
    assert me.status_code == (200 if offset < ZERO else 401), me.text


@pytest.mark.parametrize("fraction", [0.000001, 0.5, 0.999999])
def test_access_token_exp_is_whole_seconds_so_lifetime_is_at_most_the_setting(
    db_session, clock, fraction
):
    # Documented in _issued_at_and_expiry: `exp` stays whole seconds, so a
    # token issued at X.f lives (lifetime - f) seconds, never longer.
    with clock(T0 + timedelta(seconds=fraction)):
        payload = claims(create_access_token(1))
    lifetime = settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60
    assert payload["exp"] == int(payload["iat"]) + lifetime
    assert lifetime - 1 < payload["exp"] - payload["iat"] <= lifetime


@pytest.mark.parametrize("offset", EDGE_OFFSETS, ids=EDGE_IDS)
def test_refresh_token_is_refused_on_and_after_its_exp(client, db_session, clock, offset):
    user = make_user(db_session)
    with clock(T0) as frozen:
        tokens = login(client, user.email).json()
        exp = at(claims(tokens["refresh_token"])["exp"])
        assert exp == T0 + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
        frozen.move_to(exp + offset)
        response = client.post(f"{USERS}/refresh",
                               json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == (200 if offset < ZERO else 401), response.text


@pytest.mark.parametrize("offset", EDGE_OFFSETS, ids=EDGE_IDS)
def test_password_reset_link_works_for_exactly_its_lifetime(client, db_session, clock, offset):
    user = make_user(db_session)
    with clock(T0) as frozen:
        token = generate_password_reset_token(user.email, user.hashed_password)
        lifetime = timedelta(minutes=settings.PASSWORD_RESET_TOKEN_EXPIRE_MINUTES)
        assert at(claims(token)["exp"]) == T0 + lifetime
        frozen.move_to(T0 + lifetime + offset)
        response = client.post(f"{USERS}/password/reset-token/validate", json={"token": token})
    assert response.status_code == (200 if offset < ZERO else 400), response.text


@pytest.mark.parametrize("offset", EDGE_OFFSETS, ids=EDGE_IDS)
def test_email_verification_link_works_for_exactly_its_lifetime(client, db_session, clock,
                                                                 offset):
    user = make_user(db_session, verified=False)
    with clock(T0) as frozen:
        token = generate_email_verification_token(user.email)
        lifetime = timedelta(hours=settings.EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS)
        frozen.move_to(T0 + lifetime + offset)
        response = client.post(f"{USERS}/verify-email", json={"token": token})
    assert response.status_code == (200 if offset < ZERO else 400), response.text


@pytest.mark.parametrize("logout_after", [
    pytest.param(1.0, id="whole-second"),  # exp - now is whole: int() loses nothing
    pytest.param(0.001, id="1ms", marks=bug(BUG_D1)),
    pytest.param(0.5, id="500ms", marks=bug(BUG_D1)),
    pytest.param(0.999, id="999ms", marks=bug(BUG_D1)),
])
def test_bug_logout_blacklist_expires_before_the_access_token(client, db_session, clock,
                                                              logout_after):
    """Logout (with its refresh token) blacklists the access token for "its
    remaining lifetime" (blacklist_token / test_security_time). expires_in is
    int(exp - now): truncated, so the blacklist entry lapses up to a second
    BEFORE the token expires, and the logged-out token is accepted again in
    that gap. Checked 1 us before exp."""
    user = make_user(db_session)
    with clock(T0) as frozen:
        tokens = login(client, user.email).json()
        access = tokens["access_token"]
        exp = at(claims(access)["exp"])
        frozen.move_to(T0 + timedelta(seconds=logout_after))
        out = client.post(f"{USERS}/logout", json={"refresh_token": tokens["refresh_token"]},
                          headers=bearer(access))
        assert out.status_code == 200, out.text
        frozen.move_to(exp - US)
        me = client.get(f"{USERS}/me", headers=bearer(access))
    assert me.status_code == 401, "a logged-out access token was accepted before its exp"


def test_logout_everywhere_has_no_gap_before_exp(client, db_session, clock):
    # Without a refresh token the watermark revokes the token as well, so the
    # truncated blacklist TTL is covered.
    user = make_user(db_session)
    with clock(T0) as frozen:
        access = login(client, user.email).json()["access_token"]
        exp = at(claims(access)["exp"])
        frozen.move_to(T0 + timedelta(milliseconds=500))
        assert client.post(f"{USERS}/logout", headers=bearer(access)).status_code == 200
        frozen.move_to(exp - US)
        assert client.get(f"{USERS}/me", headers=bearer(access)).status_code == 401


@pytest.mark.parametrize("offset,expected", [(-US, 401), (ZERO, 200), (US, 200)],
                         ids=EDGE_IDS)
def test_sign_out_everywhere_refuses_tokens_issued_strictly_before_it(
    client, db_session, clock, offset, expected
):
    # invalidate_tokens_issued_before_now: "any token issued before now is
    # refused"; a token issued at the very same instant is not "before".
    user = make_user(db_session)
    with clock(T0 - timedelta(seconds=10)) as frozen:
        signing_out = create_access_token(user.id)
        frozen.move_to(T0)
        assert client.post(f"{USERS}/logout", headers=bearer(signing_out)).status_code == 200
        frozen.move_to(T0 + offset)
        token = create_access_token(user.id)
        frozen.move_to(T0 + timedelta(seconds=1))
        me = client.get(f"{USERS}/me", headers=bearer(token))
    assert me.status_code == expected, me.text


# =====================================================================================
# C. Rate limits through the API: N admitted, N + 1 refused, window edges.
# =====================================================================================


@pytest.mark.parametrize("limit", [1, 2, 5])
def test_default_limit_admits_exactly_n_requests(client, limits, clock, limit):
    limits(RATE_LIMIT_DEFAULT=f"{limit}/minute")
    with clock(T0):
        codes = [client.get(f"{API}/categories").status_code for _ in range(limit + 1)]
    assert codes == [200] * limit + [429]


def test_ratelimit_remaining_counts_down_to_zero_at_the_limit(client, limits, clock):
    limits(RATE_LIMIT_DEFAULT="3/minute")
    with clock(T0):
        fields = [client.get(f"{API}/categories").headers["RateLimit"] for _ in range(4)]
    assert fields == ['"default";r=2;t=60', '"default";r=1;t=60', '"default";r=0;t=60',
                      '"default";r=0;t=60']


@pytest.mark.parametrize("offset,expected", [(-MS, 429), (MS, 200)], ids=["W-1ms", "W+1ms"])
def test_fixed_window_ends_after_its_length(client, limits, clock, offset, expected):
    limits(RATE_LIMIT_DEFAULT="2/minute")
    with clock(T0) as frozen:
        for _ in range(2):
            assert client.get(f"{API}/categories").status_code == 200
        frozen.move_to(T0 + timedelta(seconds=60) + offset)
        assert client.get(f"{API}/categories").status_code == expected


@bug(BUG_D2)
def test_bug_rate_limit_client_waiting_exactly_retry_after_is_refused_again(
    client, limits, clock
):
    """A 429 says Retry-After = "seconds until the window ends" (module
    docstring, docs/API.md). A client that waits exactly that long is still
    refused: Redis (and fakeredis) keep a key alive AT its expiry instant
    (expired only when now > expireat), and the window's length is an exact
    multiple of 1000 ms, so the counter outlives Retry-After by 1 ms."""
    limits(RATE_LIMIT_DEFAULT="2/minute")
    with clock(T0) as frozen:
        for _ in range(2):
            client.get(f"{API}/categories")
        refused = client.get(f"{API}/categories")
        assert refused.status_code == 429
        wait = int(refused.headers["Retry-After"])
        frozen.move_to(T0 + timedelta(seconds=wait))
        again = client.get(f"{API}/categories")
    assert again.status_code == 200, f"refused again after waiting Retry-After={wait}s"


@pytest.mark.parametrize("elapsed,retry_after", [
    (0.0, 60), (0.001, 60), (0.999, 60), (1.0, 59), (1.001, 59), (30.5, 30), (59.0, 1),
    (59.999, 1),
])
def test_retry_after_is_the_ceiled_rest_of_the_window(client, limits, clock, elapsed,
                                                      retry_after):
    limits(RATE_LIMIT_DEFAULT="1/minute")
    with clock(T0) as frozen:
        assert client.get(f"{API}/categories").status_code == 200
        frozen.move_to(T0 + timedelta(seconds=elapsed))
        refused = client.get(f"{API}/categories")
    assert refused.status_code == 429
    assert refused.headers["Retry-After"] == str(retry_after)


@pytest.mark.parametrize("limit", [1, 3, 10])
def test_login_lock_after_exactly_n_failures_per_email_and_address(client, limits, clock,
                                                                    db_session, limit):
    # "After this many in the period, that address cannot log in to that email".
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS=f"{limit}/15 minutes")
    user = make_user(db_session)
    with clock(T0):
        for _ in range(limit):
            assert login(client, user.email, "wrong-password").status_code == 401
        locked = login(client, user.email)
    assert locked.status_code == 429


@pytest.mark.parametrize("limit", [1, 3, 10])
def test_login_n_minus_one_failures_still_lets_the_owner_in(client, limits, clock, db_session,
                                                            limit):
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS=f"{limit}/15 minutes")
    user = make_user(db_session)
    with clock(T0):
        for _ in range(limit - 1):
            assert login(client, user.email, "wrong-password").status_code == 401
        assert login(client, user.email).status_code == 200


@pytest.mark.parametrize("failures,expected", [(2, 200), (3, 429)], ids=["limit-1", "limit"])
def test_login_backstop_counts_failures_from_every_address(limits, clock, db_session, client,
                                                           failures, expected):
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL="3/hour")
    user = make_user(db_session)
    with clock(T0):
        for n in range(failures):
            visitor = https_client(f"198.51.100.{n + 1}")
            assert login(visitor, user.email, "wrong-password").status_code == 401
        owner = https_client("203.0.113.200")
        assert login(owner, user.email).status_code == expected


@pytest.mark.parametrize("offset,expected", [(-MS, 429), (MS, 200)], ids=["W-1ms", "W+1ms"])
def test_login_lock_lifts_when_its_window_ends(client, limits, clock, db_session, offset,
                                               expected):
    limits(RATE_LIMIT_LOGIN_FAILURES_PER_EMAIL_AND_ADDRESS="2/15 minutes")
    user = make_user(db_session)
    with clock(T0) as frozen:
        for _ in range(2):
            assert login(client, user.email, "wrong-password").status_code == 401
        frozen.move_to(T0 + timedelta(minutes=15) + offset)
        assert login(client, user.email).status_code == expected


@pytest.mark.parametrize("limit", [1, 3])
def test_an_address_opens_exactly_n_guest_carts(client, limits, clock, db_session, limit):
    limits(RATE_LIMIT_GUEST_CART=f"{limit}/hour")
    product = make_product(db_session)
    with clock(T0):
        codes = [https_client().post(f"{CART}/items", json={"product_id": product.id})
                 .status_code for _ in range(limit + 1)]
    assert codes == [200] * limit + [429]


@pytest.mark.parametrize("limit", [1, 2])
def test_checkout_limit_admits_exactly_n_order_attempts(client, limits, db_session, limit):
    limits(RATE_LIMIT_CHECKOUT=f"{limit}/minute")
    user = make_user(db_session)
    address = make_address(db_session, user)
    headers = auth(user.id)
    codes = []
    for _ in range(limit + 1):
        response = client.post(ORDERS, json={"shipping_address_id": address.id},
                               headers={**headers, "Idempotency-Key": uuid.uuid4().hex})
        codes.append(response.status_code)
    # The cart is empty: the route answers 400, but each attempt is counted.
    assert codes[:limit] == [400] * limit
    assert codes[limit] == 429


# =====================================================================================
# D. Cart: stock, quantity, distinct lines, TTL.
# =====================================================================================


def _add(client, headers, product_id, quantity=1, variant_id=None):
    body = {"product_id": product_id, "quantity": quantity}
    if variant_id is not None:
        body["variant_id"] = variant_id
    return client.post(f"{CART}/items", json=body, headers=headers)


@pytest.mark.parametrize("delta,expected", [(-1, 200), (0, 200), (1, 409)])
def test_add_up_to_exactly_the_stock(client, db_session, delta, expected):
    user = make_user(db_session)
    product = make_product(db_session, stock=5)
    assert _add(client, auth(user.id), product.id, 5 + delta).status_code == expected


@pytest.mark.parametrize("delta,expected", [(-1, 200), (0, 200), (1, 409)])
def test_adding_more_of_a_line_counts_what_is_already_in_the_cart(client, db_session, delta,
                                                                   expected):
    user = make_user(db_session)
    headers = auth(user.id)
    product = make_product(db_session, stock=5)
    assert _add(client, headers, product.id, 2).status_code == 200
    response = _add(client, headers, product.id, 3 + delta)
    assert response.status_code == expected
    cart = client.get(CART, headers=headers).json()
    assert cart["items"][0]["quantity"] == (5 + delta if expected == 200 else 2)


@pytest.mark.parametrize("delta,expected", [(-1, 200), (0, 200), (1, 409)])
def test_update_quantity_up_to_exactly_the_stock(client, db_session, delta, expected):
    user = make_user(db_session)
    headers = auth(user.id)
    product = make_product(db_session, stock=5)
    assert _add(client, headers, product.id, 1).status_code == 200
    response = client.put(f"{CART}/items/{product.id}", json={"quantity": 5 + delta},
                          headers=headers)
    assert response.status_code == expected


@pytest.mark.parametrize("delta,expected", [(-1, 200), (0, 200), (1, 409)])
def test_variant_stock_is_the_bound_for_a_variant_line(client, db_session, delta, expected):
    user = make_user(db_session)
    product = make_product(db_session, stock=100)
    variant = ProductVariant(product_id=product.id, sku=f"SKU-{uuid.uuid4().hex[:8]}",
                             attributes={"size": "M"}, stock_quantity=3)
    db_session.add(variant)
    db_session.commit()
    response = _add(client, auth(user.id), product.id, 3 + delta, variant.id)
    assert response.status_code == expected


@pytest.mark.parametrize("quantity,stock,expected", [
    (0, 10, 422), (1, 10, 200), (-1, 10, 422),
    (INT32_MAX, INT32_MAX, 200), (INT32_MAX + 1, INT32_MAX, 422),
])
def test_cart_quantity_bounds(client, db_session, quantity, stock, expected):
    user = make_user(db_session)
    product = make_product(db_session, stock=stock)
    assert _add(client, auth(user.id), product.id, quantity).status_code == expected


@pytest.mark.parametrize("max_lines", [1, 3])
def test_cart_holds_exactly_max_lines_distinct_items(client, db_session, monkeypatch,
                                                     max_lines):
    monkeypatch.setattr(settings, "CART_MAX_LINES", max_lines)
    user = make_user(db_session)
    headers = auth(user.id)
    products = [make_product(db_session) for _ in range(max_lines + 1)]
    for product in products[:max_lines]:
        assert _add(client, headers, product.id).status_code == 200
    full = _add(client, headers, products[max_lines].id)
    assert full.status_code == 409
    assert str(max_lines) in full.json()["detail"]
    # More of a line already there is not a new line.
    assert _add(client, headers, products[0].id).status_code == 200


@pytest.mark.parametrize("max_lines", [1, 3])
def test_guest_cart_holds_exactly_max_lines_distinct_items(client, db_session, monkeypatch,
                                                           max_lines):
    monkeypatch.setattr(settings, "CART_MAX_LINES", max_lines)
    guest = https_client()
    products = [make_product(db_session) for _ in range(max_lines + 1)]
    for product in products[:max_lines]:
        assert _add(guest, {}, product.id).status_code == 200
    assert _add(guest, {}, products[max_lines].id).status_code == 409


@pytest.mark.parametrize("mine,theirs,expected", [(1, 1, 2), (2, 1, 3), (3, 1, 3), (2, 2, 3),
                                                  (0, 3, 3), (0, 4, 3)])
def test_login_merge_fills_the_account_cart_up_to_max_lines(client, db_session, monkeypatch,
                                                            mine, theirs, expected):
    monkeypatch.setattr(settings, "CART_MAX_LINES", 3)
    user = make_user(db_session)
    for _ in range(mine):
        assert _add(client, auth(user.id), make_product(db_session).id).status_code == 200
    guest = https_client()
    for _ in range(theirs):
        # A guest cart can only reach 3 lines itself; a 4th is refused there.
        _add(guest, {}, make_product(db_session).id)
    response = login(guest, user.email)
    assert response.status_code == 200
    cart = client.get(CART, headers=bearer(response.json()["access_token"])).json()
    assert len(cart["items"]) == expected


@pytest.mark.parametrize("delta,valid", [(-1, False), (0, True), (1, True)])
def test_cart_validate_is_valid_exactly_when_stock_covers_the_quantity(client, db_session,
                                                                       delta, valid):
    user = make_user(db_session)
    headers = auth(user.id)
    product = make_product(db_session, stock=10)
    assert _add(client, headers, product.id, 4).status_code == 200
    product.stock_quantity = 4 + delta
    db_session.commit()
    body = client.post(f"{CART}/validate", headers=headers).json()
    assert body["valid"] is valid
    if not valid:
        assert body["issues"][0]["available"] == 3 and body["issues"][0]["requested"] == 4


@pytest.mark.parametrize("delta,expected", [(-1, 409), (0, 201), (1, 201)])
def test_order_takes_stock_down_to_exactly_zero(client, db_session, delta, expected):
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    product = shop.product(stock=10)
    assert shop.add_to_cart(buyer, product, 4).status_code == 200
    product.stock_quantity = 4 + delta
    db_session.commit()
    response = shop.place_order(buyer)
    assert response.status_code == expected, response.text
    assert shop.stock(product) == (delta if expected == 201 else 3)


@pytest.mark.parametrize("offset,present", [(-MS, True), (MS, False)], ids=["TTL-1ms", "TTL+1ms"])
def test_account_cart_lives_its_ttl_after_the_last_write(client, db_session, clock, fake_redis,
                                                         offset, present):
    user = make_user(db_session)
    product = make_product(db_session)
    ttl = timedelta(days=settings.REDIS_CART_TTL_DAYS)
    with clock(T0) as frozen:
        assert _add(client, auth(user.id), product.id).status_code == 200
        assert fake_redis.pttl(f"{CART_KEY_PREFIX}{user.id}") == ttl // MS
        frozen.move_to(T0 + ttl + offset)
        cart = client.get(CART, headers=auth(user.id)).json()
    assert (len(cart["items"]) == 1) is present


@pytest.mark.parametrize("offset,present", [(-MS, True), (MS, False)], ids=["TTL-1ms", "TTL+1ms"])
def test_guest_cart_and_its_cookie_expire_together(client, db_session, clock, fake_redis,
                                                   offset, present):
    product = make_product(db_session)
    ttl = timedelta(days=settings.REDIS_GUEST_CART_TTL_DAYS)
    guest = https_client()
    with clock(T0) as frozen:
        response = _add(guest, {}, product.id)
        assert response.status_code == 200
        cookie = response.headers["set-cookie"]
        assert f"Max-Age={int(ttl.total_seconds())}" in cookie
        (key,) = list(fake_redis.scan_iter(f"{GUEST_CART_KEY_PREFIX}*"))
        assert fake_redis.pttl(key) == ttl // MS
        frozen.move_to(T0 + ttl + offset)
        cart = guest.get(CART).json()
    assert (len(cart["items"]) == 1) is present


# =====================================================================================
# E. Time windows compared in SQL (both back ends, microsecond rows).
# =====================================================================================


@pytest.fixture
def buyer_row(db_session):
    user = make_user(db_session)
    return user, make_address(db_session, user)


@pytest.mark.parametrize("offset,cancelled", [(-US, True), (ZERO, False), (US, False)],
                         ids=EDGE_IDS)
def test_unpaid_order_expires_only_when_older_than_the_window(db_session, clock, buyer_row,
                                                              offset, cancelled):
    # "an order is expired when it was placed MORE than UNPAID_ORDER_EXPIRE_MINUTES
    # ago" (test_order_expiry_time); the exact-cutoff order stays.
    user, address = buyer_row
    now = T0 + timedelta(microseconds=123456)
    window = timedelta(minutes=settings.UNPAID_ORDER_EXPIRE_MINUTES)
    order = make_order(db_session, user, address, created_at=now - window + offset)
    with clock(now):
        count = tasks.expire_unpaid_orders(db_session)
    db_session.expire_all()
    assert count == (1 if cancelled else 0)
    assert db_session.get(Order, order.id).status == ("cancelled" if cancelled else "new")


@pytest.mark.parametrize("offset,cleared", [(-US, True), (ZERO, False), (US, False)],
                         ids=EDGE_IDS)
def test_idempotency_key_expires_only_when_older_than_the_ttl(db_session, clock, buyer_row,
                                                              offset, cleared):
    # "A key is honoured for IDEMPOTENCY_KEY_TTL_HOURS ... it is never shorter."
    user, address = buyer_row
    now = T0 + timedelta(microseconds=654321)
    ttl = timedelta(hours=settings.IDEMPOTENCY_KEY_TTL_HOURS)
    order = make_order(db_session, user, address, created_at=now - ttl + offset,
                       key="key-" + uuid.uuid4().hex)
    with clock(now):
        assert tasks.clear_expired_idempotency_keys(db_session) == (1 if cleared else 0)
    db_session.expire_all()
    assert (db_session.get(Order, order.id).idempotency_key is None) is cleared


@pytest.mark.parametrize("offset,replayed", [(-US, True), (ZERO, True), (US, False)],
                         ids=EDGE_IDS)
def test_order_key_is_replayed_for_exactly_its_ttl_through_the_api(client, db_session, clock,
                                                                   offset, replayed):
    shop = Shop(client, db_session)
    key = "boundary-" + uuid.uuid4().hex
    ttl = timedelta(hours=settings.IDEMPOTENCY_KEY_TTL_HOURS)
    with clock(T0) as frozen:
        buyer = shop.buyer()
        product = shop.product(stock=10)
        assert shop.add_to_cart(buyer, product).status_code == 200
        first = shop.place_order(buyer, key=key)
        assert first.status_code == 201, first.text
        frozen.move_to(T0 + ttl + offset)
        tasks.clear_expired_idempotency_keys(db_session)
        buyer.headers = auth(buyer.user_id)
        assert shop.add_to_cart(buyer, product).status_code == 200
        second = shop.place_order(buyer, key=key)
    assert second.status_code == 201, second.text
    assert (second.json()["id"] == first.json()["id"]) is replayed


class _FailingProvider(StripePaymentProvider):
    """Every lookup fails: error_payment_ids then lists exactly which payments
    the run picked, in the order it picked them."""

    def retrieve_payment_intent(self, payment_intent_id):
        raise PaymentProviderTimeoutError()


def _payment(db, order, created_at, intent=None):
    payment = Payment(order_id=order.id, provider="stripe",
                      payment_intent_id=intent or f"pi_{uuid.uuid4().hex[:20]}",
                      amount=500, currency="USD", status="succeeded",
                      created_at=created_at, updated_at=created_at)
    db.add(payment)
    db.commit()
    return payment


@pytest.mark.parametrize("edge", ["oldest", "youngest"])
@pytest.mark.parametrize("offset", EDGE_OFFSETS, ids=EDGE_IDS)
def test_reconciliation_window_edges_to_the_microsecond(db_session, buyer_row, monkeypatch,
                                                        edge, offset):
    # "payments created in the last RECONCILIATION_WINDOW_DAYS days, except
    # those younger than RECONCILIATION_MIN_AGE_MINUTES": both ends inclusive.
    monkeypatch.setattr(settings, "RECONCILIATION_MAX_CONSECUTIVE_ERRORS", 1000)
    user, address = buyer_row
    now = T0 + timedelta(microseconds=500000)
    order = make_order(db_session, user, address, created_at=now - timedelta(days=30))
    if edge == "oldest":
        moment = now - timedelta(days=settings.RECONCILIATION_WINDOW_DAYS) + offset
        inside = offset >= ZERO
    else:
        moment = now - timedelta(minutes=settings.RECONCILIATION_MIN_AGE_MINUTES) + offset
        inside = offset <= ZERO
    payment = _payment(db_session, order, moment)
    result = reconcile_payments(db_session, _FailingProvider(), now=now)
    assert result.error_payment_ids == ([payment.id] if inside else [])


@pytest.mark.parametrize("count,truncated", [(2, False), (3, False), (4, True)])
def test_reconciliation_cap_truncates_only_beyond_it(db_session, buyer_row, monkeypatch, count,
                                                     truncated):
    monkeypatch.setattr(settings, "RECONCILIATION_MAX_PAYMENTS", 3)
    monkeypatch.setattr(settings, "RECONCILIATION_MAX_CONSECUTIVE_ERRORS", 1000)
    user, address = buyer_row
    now = T0
    order = make_order(db_session, user, address, created_at=now - timedelta(days=3))
    payments = [_payment(db_session, order, now - timedelta(days=1)) for _ in range(count)]
    result = reconcile_payments(db_session, _FailingProvider(), now=now)
    assert result.truncated is truncated
    assert result.error_payment_ids == [p.id for p in reversed(payments)][:3]


@pytest.mark.parametrize("failures,aborted", [(2, False), (3, True), (4, True)])
def test_reconciliation_stops_after_exactly_n_errors_in_a_row(db_session, buyer_row,
                                                              monkeypatch, failures, aborted):
    monkeypatch.setattr(settings, "RECONCILIATION_MAX_CONSECUTIVE_ERRORS", 3)
    user, address = buyer_row
    order = make_order(db_session, user, address, created_at=T0 - timedelta(days=3))
    for _ in range(failures):
        _payment(db_session, order, T0 - timedelta(days=1))
    result = reconcile_payments(db_session, _FailingProvider(), now=T0)
    assert result.aborted is aborted
    assert result.errors == min(failures, 3)


def _in_flight_attempt(db, order_id, created_at, *, pending_id=False):
    intent = (f"pending-{uuid.uuid4().hex}" if pending_id
              else f"pi_{uuid.uuid4().hex[:20]}")
    payment = Payment(order_id=order_id, provider="stripe", payment_intent_id=intent,
                      amount=1000, currency="USD",
                      status="processing" if pending_id else "requires_action",
                      created_at=created_at, updated_at=created_at)
    db.add(payment)
    db.commit()
    return payment


@pytest.mark.parametrize("offset,blocked", [(-US, False), (US, True)], ids=["T-1us", "T+1us"])
def test_an_in_flight_attempt_blocks_another_until_it_is_stale(client, db_session, clock,
                                                               offset, blocked):
    shop = Shop(client, db_session)
    stale = timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES)
    now = T0 + timedelta(microseconds=250000)
    with clock(now):
        buyer = shop.buyer()
        order = shop.order_of(buyer, shop.product(price="10.00"))
        _in_flight_attempt(db_session, order["id"], now - stale + offset)
        response = shop.pay(buyer, order["id"], token=PAY_OK)
    assert response.status_code == (409 if blocked else 200), response.text


def test_attempt_exactly_stale_after_minutes_old_is_stale(client, db_session,
                                                          clock):
    """POLICY (D3): an attempt exactly PAYMENT_ATTEMPT_STALE_AFTER_MINUTES old
    no longer blocks (created_at > now - window); the existing suite pins the
    same edge (tests/integration/test_payment_webhook_mutants.py::
    test_an_attempt_exactly_at_the_stale_limit_does_not). The window is closed
    at its far end: "stale" begins AT the limit, not 1 us after."""
    shop = Shop(client, db_session)
    stale = timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES)
    now = T0 + timedelta(microseconds=250000)
    with clock(now):
        buyer = shop.buyer()
        order = shop.order_of(buyer, shop.product(price="10.00"))
        _in_flight_attempt(db_session, order["id"], now - stale)
        response = shop.pay(buyer, order["id"], token=PAY_OK)
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("offset,holds", [(-US, False), (US, True)], ids=["T-1us", "T+1us"])
def test_an_event_waits_for_an_attempt_still_storing_its_id_until_stale(db_session, clock,
                                                                        buyer_row, offset,
                                                                        holds):
    user, address = buyer_row
    stale = timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES)
    now = T0 + timedelta(microseconds=750000)
    order = make_order(db_session, user, address, created_at=now - timedelta(hours=1))
    _in_flight_attempt(db_session, order.id, now - stale + offset, pending_id=True)
    with clock(now):
        result = stripe_events._attempt_still_storing_its_id(
            db_session, {"metadata": {"order_id": str(order.id)}})
    assert result is holds


def test_event_not_held_back_by_attempt_exactly_stale_after_minutes_old(db_session, clock,
                                                                        buyer_row):
    """POLICY (D3, second site: stripe_events._attempt_still_storing_its_id): an
    attempt exactly that old is already stale and does not hold events back -
    pinned by tests/integration/test_payment_webhook_mutants.py::
    test_an_attempt_exactly_at_the_stale_limit_does_not."""
    user, address = buyer_row
    stale = timedelta(minutes=settings.PAYMENT_ATTEMPT_STALE_AFTER_MINUTES)
    now = T0 + timedelta(microseconds=750000)
    order = make_order(db_session, user, address, created_at=now - timedelta(hours=1))
    _in_flight_attempt(db_session, order.id, now - stale, pending_id=True)
    with clock(now):
        assert stripe_events._attempt_still_storing_its_id(
            db_session, {"metadata": {"order_id": str(order.id)}}) is False


@pytest.mark.parametrize("order_id", ["0", "-1", str(2**31), str(2**63), "1.5", "", None])
def test_event_order_id_outside_int32_is_never_looked_up(db_session, order_id):
    statements = []

    def _count(*_args):
        statements.append(1)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", _count)
    try:
        assert stripe_events._attempt_still_storing_its_id(
            db_session, {"metadata": {"order_id": order_id}}) is False
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert statements == [], "an order id the INTEGER column cannot hold was queried"


@pytest.mark.parametrize("order_id", ["1", str(2**31 - 1)])
def test_event_order_id_at_the_int32_edges_is_looked_up(db_session, order_id):
    # No such order: False, but no error from an id the column can hold.
    assert stripe_events._attempt_still_storing_its_id(
        db_session, {"metadata": {"order_id": order_id}}) is False


def _stuck_event(db, updated_at, status="received"):
    event_id = f"evt_{uuid.uuid4().hex[:24]}"
    db.add(WebhookEvent(event_id=event_id, event_type="payment_intent.succeeded",
                        status=status,
                        payload=json.dumps({"id": event_id, "data": {"object": {}}}),
                        created_at=updated_at, updated_at=updated_at))
    db.commit()
    return event_id


@pytest.fixture
def published(monkeypatch):
    sent = []

    def _enqueue(event_id):
        sent.append(event_id)
        return True

    monkeypatch.setattr(stripe_events, "enqueue_event", _enqueue)
    return sent


@pytest.mark.parametrize("offset,swept", [(-US, True), (ZERO, False), (US, False)],
                         ids=EDGE_IDS)
def test_sweeper_publishes_events_older_than_the_sweep_age(db_session, clock, published, offset,
                                                           swept):
    # Ambiguous at T ("have not changed for this long"): the code's strict
    # "older than" is asserted, as for the unpaid-order window.
    now = T0 + timedelta(microseconds=999999)
    age = timedelta(minutes=settings.STRIPE_EVENT_SWEEP_AFTER_MINUTES)
    event_id = _stuck_event(db_session, now - age + offset)
    with clock(now):
        stripe_events.enqueue_stuck_events(db_session)
    assert (event_id in published) is swept


@pytest.mark.parametrize("count", [2, 3, 4])
def test_sweeper_batch_is_capped_at_exactly_the_setting(db_session, clock, published,
                                                        monkeypatch, count):
    monkeypatch.setattr(settings, "STRIPE_EVENT_SWEEP_BATCH", 3)
    for n in range(count):
        _stuck_event(db_session, T0 - timedelta(hours=2, minutes=n))
    with clock(T0):
        assert stripe_events.enqueue_stuck_events(db_session) == min(count, 3)


@pytest.mark.parametrize("offset,purged", [(-US, True), (ZERO, True), (US, False)],
                         ids=EDGE_IDS)
def test_erasure_runs_once_the_grace_period_has_passed(db_session, clock, offset, purged):
    # "performs the actual hard delete once DATA_DELETION_GRACE_PERIOD_DAYS has
    # passed" (gdpr.process_data_deletion): at exactly the end it has passed.
    now = T0 + timedelta(microseconds=1)
    grace = timedelta(days=settings.DATA_DELETION_GRACE_PERIOD_DAYS)
    user = make_user(db_session, active=False, data_deletion_requested=True,
                     data_deletion_date=now - grace + offset)
    with clock(now):
        assert tasks.purge_expired_deletion_requests(db_session) == (1 if purged else 0)
    db_session.expire_all()
    assert db_session.get(User, user.id).email.startswith("deleted-user-") is purged


@pytest.mark.parametrize("offset,purged", [(-US, True), (ZERO, False), (US, False)],
                         ids=EDGE_IDS)
def test_retention_purge_needs_more_than_the_retention_period(db_session, clock, offset, purged):
    now = T0 + timedelta(microseconds=424242)
    last_seen = now - timedelta(days=settings.USER_DATA_RETENTION_DAYS) + offset
    make_user(db_session, active=False, last_login=last_seen,
              last_activity_date=last_seen, created_at=last_seen)
    with clock(now):
        assert tasks.purge_expired_retention(db_session) == (1 if purged else 0)


@pytest.mark.parametrize("period,days", [(30, 30), (1, 1)])
@pytest.mark.parametrize("offset,purged", [(-US, True), (US, False)], ids=["T-1us", "T+1us"])
def test_retention_purge_uses_the_users_own_period(db_session, clock, period, days, offset,
                                                   purged):
    now = T0
    last_seen = now - timedelta(days=days) + offset
    make_user(db_session, active=False, last_login=last_seen, last_activity_date=last_seen,
              created_at=last_seen, data_retention_period=period)
    with clock(now):
        assert tasks.purge_expired_retention(db_session) == (1 if purged else 0)


@pytest.mark.parametrize("age,within", [
    (timedelta(days=365) - US, True),
    (timedelta(days=365), True),
])
def test_data_retention_status_inside_the_period(client, db_session, clock, age, within):
    now = T0
    user = make_user(db_session, created_at=now - age)
    with clock(now):
        body = client.get(f"{API}/legal/data-retention", headers=auth(user.id)).json()
    assert body["within_retention_period"] is within


@bug(BUG_D4)
@pytest.mark.parametrize("past", [US, timedelta(hours=1), timedelta(hours=23, minutes=59)],
                         ids=["1us", "1h", "23h59m"])
def test_bug_data_retention_status_truncates_age_to_whole_days(client, db_session, clock,
                                                               past):
    """GET /legal/data-retention ("Check if user data is within the retention
    period") computes (now - created_at).days <= retention: the age is floored
    to whole days, so an account up to 23h59m59.999999s PAST its 365 days is
    still reported "within" - while purge_expired_retention treats anything
    past `timedelta(days=365)` as expired."""
    now = T0
    user = make_user(db_session, created_at=now - timedelta(days=365) - past)
    with clock(now):
        body = client.get(f"{API}/legal/data-retention", headers=auth(user.id)).json()
    assert body["within_retention_period"] is False


ADMIN_TZ = ["+00:00", "+05:30", "-08:00"]


def _iso(moment, tz):
    sign = 1 if tz[0] == "+" else -1
    hours, minutes = int(tz[1:3]), int(tz[4:6])
    shift = timedelta(hours=hours, minutes=minutes) * sign
    return (moment + shift).replace(tzinfo=None).isoformat(timespec="microseconds") + tz


@pytest.mark.parametrize("tz", ADMIN_TZ)
@pytest.mark.parametrize("bound", ["start_date", "end_date"])
def test_admin_order_date_filters_are_inclusive_to_the_microsecond(client, db_session,
                                                                   buyer_row, tz, bound):
    user, address = buyer_row
    admin = make_user(db_session, role="admin")
    edge = T0 + timedelta(microseconds=777777)
    ids = {name: make_order(db_session, user, address, created_at=edge + offset).id
           for offset, name in EDGE}
    response = client.get(f"{API}/admin/orders", params={bound: _iso(edge, tz), "size": 100},
                          headers=auth(admin.id))
    assert response.status_code == 200, response.text
    found = {o["id"] for o in response.json()["items"]}
    if bound == "start_date":
        assert found == {ids["T"], ids["T+1us"]}
    else:
        assert found == {ids["T-1us"], ids["T"]}


@pytest.mark.parametrize("offset,counted", [(-US, False), (ZERO, True), (US, True)],
                         ids=EDGE_IDS)
def test_admin_stats_24h_window_edge(client, db_session, clock, buyer_row, offset, counted):
    user, address = buyer_row
    admin = make_user(db_session, role="admin")
    now = T0 + timedelta(microseconds=31337)
    make_order(db_session, user, address, created_at=now - timedelta(hours=24) + offset,
               status="confirmed", payment_status="paid")
    with clock(now):
        body = client.get(f"{API}/admin/stats", params={"period": "24h"},
                          headers=auth(admin.id)).json()
    assert body["order_count"] == (1 if counted else 0)


def _signed_webhook(client, timestamp):
    body = json.dumps({"id": f"evt_{uuid.uuid4().hex[:24]}", "object": "event",
                       "type": "payment_intent.succeeded", "created": timestamp,
                       "data": {"object": {"id": "pi_x", "object": "payment_intent"}}}).encode()
    signature = StripePaymentProvider.sign_payload(
        body, settings.STRIPE_WEBHOOK_SECRET.get_secret_value(), timestamp=timestamp)
    return client.post(f"{ORDERS}/webhooks/stripe", content=body,
                       headers={"Stripe-Signature": signature,
                                "Content-Type": "application/json"})


@pytest.mark.parametrize("age,expected", [(299, 200), (300, 200), (301, 400)])
def test_webhook_signature_tolerance_is_inclusive_like_stripes(client, clock, age, expected):
    # stripe.WebhookSignature.verify_header: refused when timestamp < now - tolerance.
    tolerance = settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS
    assert tolerance == 300
    with clock(T0):
        response = _signed_webhook(client, int(T0.timestamp()) - age)
    assert response.status_code == expected, response.text


@pytest.mark.parametrize("fraction,expected", [(0.0, 200), (0.000001, 400), (0.5, 400)])
def test_webhook_signature_tolerance_at_sub_second_now(client, clock, fraction, expected):
    tolerance = settings.STRIPE_WEBHOOK_TOLERANCE_SECONDS
    with clock(T0 + timedelta(seconds=fraction)):
        response = _signed_webhook(client, int(T0.timestamp()) - tolerance)
    assert response.status_code == expected, response.text


# =====================================================================================
# F. Request bounds (lengths, pages, prices) at limit - 1 / limit / limit + 1.
# =====================================================================================


@pytest.mark.parametrize("length,expected", [(7, 400), (8, 201), (9, 201), (254, 201),
                                             (255, 201), (256, 400)])
def test_idempotency_key_length_bounds(client, db_session, length, expected):
    # docs/API.md: "Printable ASCII string between 8 and 255 characters.
    # Missing or out-of-range headers immediately receive 400".
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    assert shop.add_to_cart(buyer, shop.product()).status_code == 200
    key = (uuid.uuid4().hex * 8)[:length]
    response = shop.place_order(buyer, key=key)
    assert response.status_code == expected, response.text


@pytest.mark.parametrize("password,expected", [
    ("aB3$xyz", 422),                      # 7 characters
    ("aB3$xyzQ", 202),                     # 8
    ("aB3$xyzQr", 202),                    # 9
    ("Qz9!" + "k" * 68, 202),              # 72 bytes
    ("Qz9!" + "k" * 69, 422),              # 73 bytes
    ("Qz9!" + "k" * 66 + "é", 202),  # 72 bytes, the last 2-byte char ends it
    ("Qz9!" + "k" * 67 + "é", 422),  # 73 bytes in 72 characters
])
def test_password_length_bounds(client, valid_registration_payload, password, expected):
    payload = {**valid_registration_payload, "password": password,
               "email": f"p-{uuid.uuid4().hex[:8]}@example.com"}
    response = client.post(f"{USERS}/register", json=payload)
    assert response.status_code == expected, response.text


@pytest.mark.parametrize("params,expected", [
    ({"size": 0}, 422), ({"size": 1}, 200), ({"size": 100}, 200), ({"size": 101}, 422),
    ({"page": 0}, 422), ({"page": 1}, 200), ({"page": INT32_MAX}, 200),
    ({"page": INT32_MAX + 1}, 422),
])
def test_pagination_bounds(client, params, expected):
    assert client.get(f"{API}/products", params=params).status_code == expected


@pytest.mark.parametrize("length,expected", [(2, 422), (3, 200), (100, 200), (101, 422)])
def test_search_query_length_bounds(client, length, expected):
    response = client.get(f"{API}/products/search", params={"query": "q" * length})
    assert response.status_code == expected


@pytest.mark.parametrize("bound,value,included", [
    ("min_price", "9.99", True), ("min_price", "10.00", True), ("min_price", "10.01", False),
    ("max_price", "9.99", False), ("max_price", "10.00", True), ("max_price", "10.01", True),
])
def test_price_filters_are_inclusive_to_the_cent(client, db_session, bound, value, included):
    product = make_product(db_session, price="10.00")
    body = client.get(f"{API}/products", params={bound: value}).json()
    assert (product.id in {p["id"] for p in body["items"]}) is included


@pytest.mark.parametrize("stock,listed", [(0, False), (1, True)])
def test_in_stock_filter_starts_at_one_unit(client, db_session, stock, listed):
    product = make_product(db_session, stock=stock)
    body = client.get(f"{API}/products", params={"in_stock": True}).json()
    assert (product.id in {p["id"] for p in body["items"]}) is listed


@pytest.mark.parametrize("count,has_more", [(2, False), (3, False), (4, True)])
def test_has_more_is_true_only_beyond_the_page(client, db_session, count, has_more):
    for _ in range(count):
        make_product(db_session)
    body = client.get(f"{API}/products", params={"size": 3, "page": 1}).json()
    assert body["total"] == count
    assert body["has_more"] is has_more
    assert len(body["items"]) == min(count, 3)


# =====================================================================================
# G. Leftover arithmetic sanity (time module vs datetime precision).
# =====================================================================================


def test_frozen_time_resolves_a_microsecond_at_the_suites_instant(clock):
    # Every +-1us case above relies on time.time() telling T-1us, T and T+1us
    # apart at T0 (a float at ~1.9e9 s has ~0.24 us resolution).
    readings = []
    for offset in EDGE_OFFSETS:
        with clock(T0 + offset):
            readings.append(time.time())
    assert readings[0] < readings[1] < readings[2]
    assert math.isclose(readings[2] - readings[0], 2e-6, abs_tol=5e-7)


def test_cart_ttl_comes_from_the_settings(monkeypatch):
    monkeypatch.setattr(settings, "REDIS_CART_TTL_DAYS", 3)
    monkeypatch.setattr(settings, "REDIS_GUEST_CART_TTL_DAYS", 2)
    assert redis_codec.cart_ttl(CartOwner.account(1)) == timedelta(days=3)
    assert redis_codec.cart_ttl(CartOwner.guest()) == timedelta(days=2)




# --- the order total against Order.total_amount, Numeric(10, 2) -----------------------

ORDER_TOTAL_CASES = [
    ([("99999999.99", 1)], "99999999.99"),            # the largest total the column holds
    ([("49999999.99", 2)], "99999999.98"),
    ([("99999999.98", 1), ("0.01", 1)], "99999999.99"),
]


@pytest.mark.parametrize("lines,total", ORDER_TOTAL_CASES)
def test_order_total_up_to_the_largest_numeric_10_2(client, db_session, lines, total):
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    for price, quantity in lines:
        assert shop.add_to_cart(buyer, shop.product(price=price), quantity).status_code == 200
    response = shop.place_order(buyer)
    assert response.status_code == 201, response.text
    assert response.json()["total_amount"] == total


@bug(BUG_D5)
@pytest.mark.parametrize("lines", [
    [("50000000.00", 2)],                 # one cent past: 100000000.00
    [("99999999.99", 1), ("0.01", 1)],
    [("99999999.99", 2)],
], ids=["qty-x-price", "two-lines", "far-past"])
def test_bug_order_total_past_numeric_10_2_is_not_refused_cleanly(client, db_session, lines):
    """app/schemas/order.py: "Order.total_amount is Numeric(10, 2), so no
    amount paid or refunded for an order has more digits than that", and an
    amount that does not fit "used to answer 500 (... the database refuses
    the value)" (test_catalog_money). Every price fits, but their sum is
    never checked: one cent past 99999999.99 the order is a 500 on
    PostgreSQL (numeric field overflow) and, on SQLite, a stored order whose
    total no payment request can even name (Money: max_digits=10). Expected:
    a 4xx refusal, no order, no stock taken - on both back ends."""
    shop = Shop(client, db_session)
    buyer = shop.buyer()
    products = []
    for price, quantity in lines:
        product = shop.product(price=price)
        products.append(product)
        assert shop.add_to_cart(buyer, product, quantity).status_code == 200
    response = shop.place_order(buyer)
    db_session.expire_all()
    assert 400 <= response.status_code < 500, (response.status_code, response.text[:200])
    assert db_session.query(Order).count() == 0
    assert [shop.stock(p) for p in products] == [10] * len(products)


# --- refunds against the charge: amount - 1 cent, amount, amount + 1 cent ---------------


def _paid_order(shop, price="10.00"):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product(price=price))
    response = shop.pay(buyer, order["id"], token=PAY_OK)
    assert response.status_code == 200, response.text
    return order["id"]


@pytest.mark.parametrize("amounts,codes,status", [
    (["9.99"], [200], "paid"),
    (["10.00"], [200], "refunded"),
    (["10.01"], [400], "paid"),
    (["4.00", "5.99"], [200, 200], "paid"),
    (["4.00", "6.00"], [200, 200], "refunded"),
    (["4.00", "6.01"], [200, 400], "paid"),
    (["9.99", "0.01"], [200, 200], "refunded"),
    (["9.99", "0.02"], [200, 400], "paid"),
])
def test_refunds_cover_the_charge_exactly_at_its_amount(client, db_session, amounts, codes,
                                                        status):
    shop = Shop(client, db_session)
    order_id = _paid_order(shop)
    admin = auth(make_user(db_session, role="admin").id)
    got = [shop.refund(admin, order_id, amount=a).status_code for a in amounts]
    assert got == codes
    assert shop.order(order_id).payment_status == status


@pytest.mark.parametrize("ahead,expected", [(299, 200), (300, 200), (301, 400)])
def test_webhook_signature_from_the_future_is_bounded_by_the_same_tolerance(client, clock, ahead,
                                                                            expected):
    with clock(T0):
        response = _signed_webhook(client, int(T0.timestamp()) + ahead)
    assert response.status_code == expected, response.text


@pytest.mark.parametrize("period,window", [("7d", timedelta(days=7)), ("30d", timedelta(days=30))])
@pytest.mark.parametrize("offset,counted", [(-US, False), (ZERO, True), (US, True)],
                         ids=EDGE_IDS)
def test_admin_stats_longer_window_edges(client, db_session, clock, buyer_row, period, window,
                                         offset, counted):
    user, address = buyer_row
    admin = make_user(db_session, role="admin")
    now = T0 + timedelta(microseconds=271828)
    make_order(db_session, user, address, created_at=now - window + offset,
               status="confirmed", payment_status="paid")
    with clock(now):
        body = client.get(f"{API}/admin/stats", params={"period": period},
                          headers=auth(admin.id)).json()
    assert body["order_count"] == (1 if counted else 0)


@pytest.mark.parametrize("count,on_page_two", [(3, 0), (4, 1), (6, 3), (7, 3)])
def test_second_page_holds_what_the_first_did_not(client, db_session, count, on_page_two):
    for _ in range(count):
        make_product(db_session)
    body = client.get(f"{API}/products", params={"size": 3, "page": 2}).json()
    assert len(body["items"]) == on_page_two
    assert body["has_more"] is (count > 6)
