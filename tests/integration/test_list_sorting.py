"""sort_by / sort_order work on the lists that declare them (F-33).

Each list has an allowlist of public field names; the id breaks ties in the
direction of the sort; no sort_by (or one the list does not know) is the
old order of the list, and is not an error, so a client that sent such a
value while it was ignored keeps working.
"""
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from payment_support import PAY_OK, Shop, reset_provider

from app.core.security import create_access_token, get_password_hash
from app.models.address import Address
from app.models.category import Category
from app.models.enums import UserRole
from app.models.order import Order
from app.models.payment import WebhookEvent
from app.models.payment_discrepancy import PaymentDiscrepancy
from app.models.user import User

API = "/api/v1"
T0 = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
# One value per row; the repeats are the ties the id has to break.
VALUES = [2, 1, 2, 3, 1]


@pytest.fixture(autouse=True)
def _clean_provider():
    reset_provider()
    yield
    reset_provider()


def _ids(client, url, headers=None, **params):
    response = client.get(url, params=params, headers=headers)
    assert response.status_code == 200, response.text
    return [row["id"] for row in response.json()["items"]]


def _expected(rows, descending):
    """rows: (id, value) pairs, ordered by value then id, both in one direction."""
    return [i for i, _ in sorted(rows, key=lambda r: (r[1], r[0]), reverse=descending)]


def _check(client, url, rows, field, headers=None):
    for order, descending in (("asc", False), ("desc", True)):
        got = _ids(client, url, headers, sort_by=field, sort_order=order, size=100)
        assert got == _expected(rows, descending), (field, order)
        # Read one row per page, the pages fit together too.
        paged = []
        for page in range(1, len(rows) + 2):
            paged += _ids(client, url, headers, sort_by=field, sort_order=order, size=1, page=page)
        assert paged == got
    # sort_order left out: ascending
    assert _ids(client, url, headers, sort_by=field, size=100) == _expected(rows, False)


# -- categories ----------------------------------------------------------------------

@pytest.fixture
def categories(db_session):
    rows = [Category(name=f"Cat {v}", level=0, path=[], created_at=T0 + timedelta(hours=v))
            for v in VALUES]
    db_session.add_all(rows)
    db_session.commit()
    return rows


@pytest.mark.parametrize("field", ["name", "created_at"])
def test_categories_sort_by_each_field(client, categories, field):
    _check(client, f"{API}/categories", [(c.id, getattr(c, field)) for c in categories], field)


def test_categories_sort_by_id(client, categories):
    ids = sorted(c.id for c in categories)
    assert _ids(client, f"{API}/categories", sort_by="id", sort_order="desc") == ids[::-1]
    assert _ids(client, f"{API}/categories", sort_by="id") == ids


@pytest.mark.parametrize("params", [
    {}, {"sort_by": "nope"}, {"sort_order": "desc"}, {"sort_by": "name; DROP TABLE categories"},
    {"sort_by": "description"},
])
def test_categories_default_order_and_unknown_fall_back(client, categories, params):
    assert _ids(client, f"{API}/categories", **params) == sorted(c.id for c in categories)


def test_a_bad_sort_order_is_still_422(client, categories):
    assert client.get(f"{API}/categories", params={"sort_order": "up"}).status_code == 422


# -- addresses and orders of a customer ------------------------------------------------

@pytest.fixture
def buyer(db_session):
    user = User(
        email="buyer@example.com", hashed_password=get_password_hash("Str0ngPass1"),
        full_name="Someone", role=UserRole.CLIENT, gdpr_consent=True,
        privacy_policy_accepted=True, consent_history=[], is_active=True, is_email_verified=True)
    db_session.add(user)
    db_session.commit()
    return user, {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def addresses(db_session, buyer):
    rows = [Address(user_id=buyer[0].id, street=f"{i} Main St", city="Town", state="ST",
                    postal_code="10001", country="US", created_at=T0 + timedelta(hours=v),
                    updated_at=T0) for i, v in enumerate(VALUES)]
    db_session.add_all(rows)
    db_session.commit()
    return rows


def test_addresses_sort_by_created_at(client, buyer, addresses):
    _check(client, f"{API}/users/addresses", [(a.id, a.created_at) for a in addresses],
           "created_at", buyer[1])


def test_addresses_sort_by_id(client, buyer, addresses):
    ids = sorted(a.id for a in addresses)
    assert _ids(client, f"{API}/users/addresses", buyer[1], sort_by="id", sort_order="desc") == ids[::-1]


@pytest.mark.parametrize("params", [{}, {"sort_by": "street"}, {"sort_by": ""}])
def test_addresses_default_order(client, buyer, addresses, params):
    assert _ids(client, f"{API}/users/addresses", buyer[1], **params) == sorted(a.id for a in addresses)


@pytest.fixture
def orders(db_session, buyer, addresses):
    rows = [Order(user_id=buyer[0].id, total_amount=Decimal(f"{v}0.00"),
                  shipping_address_id=addresses[0].id,
                  created_at=T0 + timedelta(hours=v), updated_at=T0) for v in VALUES]
    db_session.add_all(rows)
    db_session.commit()
    return rows


@pytest.mark.parametrize("field", ["total_amount", "created_at"])
def test_user_orders_sort_by_each_field(client, buyer, orders, field):
    _check(client, f"{API}/users/orders", [(o.id, getattr(o, field)) for o in orders],
           field, buyer[1])


def test_user_orders_sort_by_id(client, buyer, orders):
    ids = sorted(o.id for o in orders)
    assert _ids(client, f"{API}/users/orders", buyer[1], sort_by="id") == ids


@pytest.mark.parametrize("params", [{}, {"sort_by": "status"}, {"sort_order": "asc"}])
def test_user_orders_default_is_newest_first(client, buyer, orders, params):
    rows = [(o.id, o.created_at) for o in orders]
    assert _ids(client, f"{API}/users/orders", buyer[1], **params) == _expected(rows, True)


# -- admin: webhook events -----------------------------------------------------------------

@pytest.fixture
def shop(client, db_session):
    return Shop(client, db_session)


def _event(shop, status, when):
    event_id = f"evt_{uuid.uuid4().hex[:24]}"
    row = WebhookEvent(event_id=event_id, event_type="payment_intent.succeeded", status=status,
                       payload=json.dumps({"id": event_id}), created_at=when, updated_at=when)
    shop.db.add(row)
    shop.db.commit()
    return row


@pytest.fixture
def events(shop):
    statuses = ["received", "failed", "received", "processed", "failed"]
    return [_event(shop, s, T0 + timedelta(hours=v)) for s, v in zip(statuses, VALUES, strict=True)]


@pytest.mark.parametrize("field", ["status", "created_at"])
def test_webhook_events_sort_by_each_field(shop, events, field):
    _check(shop.client, f"{API}/admin/webhook-events",
           [(e.id, getattr(e, field)) for e in events], field, shop.admin())


def test_webhook_events_sort_by_id(shop, events):
    ids = sorted(e.id for e in events)
    assert _ids(shop.client, f"{API}/admin/webhook-events", shop.admin(),
                sort_by="id", sort_order="desc") == ids[::-1]


@pytest.mark.parametrize("params", [{}, {"sort_by": "event_type"}, {"sort_order": "asc"}])
def test_webhook_events_default_is_newest_first(shop, events, params):
    got = _ids(shop.client, f"{API}/admin/webhook-events", shop.admin(), **params)
    assert got == _expected([(e.id, e.created_at) for e in events], True)


# -- admin: reconciliation discrepancies ----------------------------------------------------

@pytest.fixture
def discrepancies(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product(price="25.00"))
    assert shop.pay(buyer, order["id"], token=PAY_OK).status_code == 200
    (payment,) = shop.payments(order["id"])
    kinds = ["status", "amount", "status", "currency", "amount"]
    rows = [PaymentDiscrepancy(payment_id=payment.id, kind=k, subject=str(i), ours="1", theirs="2",
                               detected_at=T0 + timedelta(hours=v),
                               last_seen_at=T0 + timedelta(hours=10 - v))
            for i, (k, v) in enumerate(zip(kinds, VALUES, strict=True))]
    shop.db.add_all(rows)
    shop.db.commit()
    return rows


@pytest.mark.parametrize("field", ["detected_at", "last_seen_at", "kind"])
def test_discrepancies_sort_by_each_field(shop, discrepancies, field):
    _check(shop.client, f"{API}/admin/reconciliation/discrepancies",
           [(d.id, getattr(d, field)) for d in discrepancies], field, shop.admin())


def test_discrepancies_sort_by_id(shop, discrepancies):
    ids = sorted(d.id for d in discrepancies)
    assert _ids(shop.client, f"{API}/admin/reconciliation/discrepancies", shop.admin(),
                sort_by="id", sort_order="desc") == ids[::-1]


@pytest.mark.parametrize("params", [{}, {"sort_by": "subject"}, {"sort_order": "desc"}])
def test_discrepancies_default_is_oldest_first(shop, discrepancies, params):
    got = _ids(shop.client, f"{API}/admin/reconciliation/discrepancies", shop.admin(), **params)
    assert got == _expected([(d.id, d.detected_at) for d in discrepancies], False)


def test_the_sort_fields_are_documented_in_openapi(client):
    spec = client.get("/api/v1/openapi.json").json()
    for path, field in ((f"{API}/users/orders", "total_amount"), (f"{API}/users/addresses", "created_at")):
        params = {p["name"]: p for p in spec["paths"][path]["get"]["parameters"]}
        assert field in params["sort_by"]["description"]
        assert "id" in params["sort_order"]["description"]
    for path in (f"{API}/categories", f"{API}/admin/webhook-events",
                 f"{API}/admin/reconciliation/discrepancies"):
        assert "sort_by (" in spec["paths"][path]["get"]["description"]
