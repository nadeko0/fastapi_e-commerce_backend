"""
The admin endpoints of the payment reconciliation: the open discrepancies
(paginated, optionally of one order) and a run on demand (one order inline,
everything queued for the worker). Admin only.
"""
from datetime import datetime, timedelta

import pytest
from payment_support import PAY_OK, Shop, reset_provider

from app.api.v1 import admin
from app.models.payment_discrepancy import PaymentDiscrepancy

BASE = "/api/v1/admin/reconciliation"


@pytest.fixture(autouse=True)
def _clean_provider():
    reset_provider()
    yield
    reset_provider()


@pytest.fixture
def shop(client, db_session):
    return Shop(client, db_session)


def _paid(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product(price="25.00"))
    assert shop.pay(buyer, order["id"], token=PAY_OK).status_code == 200
    (payment,) = shop.payments(order["id"])
    return order, payment


def _discrepancy(db, payment, kind="amount", resolved=False, detected_at=None, subject=""):
    row = PaymentDiscrepancy(
        payment_id=payment.id, kind=kind, subject=subject, ours="1", theirs="2",
        detected_at=detected_at or datetime.utcnow(), last_seen_at=datetime.utcnow(),
        resolved_at=datetime.utcnow() if resolved else None)
    db.add(row)
    db.commit()
    return row


# -- permissions ------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("get", "/discrepancies"), ("post", "/run"), ("post", "/run?order_id=1"),
])
def test_only_an_admin_may_use_it(shop, client, method, path):
    buyer = shop.buyer()

    assert getattr(client, method)(BASE + path).status_code == 401
    assert getattr(client, method)(BASE + path, headers=buyer.headers).status_code == 403


# -- the list ---------------------------------------------------------------------------

def test_no_discrepancies_is_an_empty_page(shop, client):
    response = client.get(f"{BASE}/discrepancies", headers=shop.admin())

    assert response.status_code == 200
    assert response.json()["data"] == {
        "items": [], "total": 0, "page": 1, "size": 20, "has_more": False}


def test_open_discrepancies_are_listed_oldest_first_with_their_order(shop, client):
    order, payment = _paid(shop)
    old = _discrepancy(shop.db, payment, "status", detected_at=datetime(2026, 10, 1))
    new = _discrepancy(shop.db, payment, "amount", detected_at=datetime(2026, 10, 2))
    _discrepancy(shop.db, payment, "currency", resolved=True)

    response = client.get(f"{BASE}/discrepancies", headers=shop.admin())

    data = response.json()["data"]
    assert [item["id"] for item in data["items"]] == [old.id, new.id]
    assert data["total"] == 2
    first = data["items"][0]
    assert first == {
        "id": old.id, "order_id": order["id"], "payment_id": payment.id,
        "transaction_id": payment.payment_intent_id, "kind": "status", "subject": "",
        "ours": "1", "theirs": "2", "detected_at": "2026-10-01T00:00:00",
        "last_seen_at": first["last_seen_at"],
    }


def test_the_list_is_paginated(shop, client):
    _, payment = _paid(shop)
    ids = [_discrepancy(shop.db, payment, kind=f"k{i}",
                        detected_at=datetime(2026, 10, 1) + timedelta(hours=i)).id
           for i in range(5)]
    admin_headers = shop.admin()

    pages = [client.get(f"{BASE}/discrepancies", params={"page": p, "size": 2},
                        headers=admin_headers).json()["data"] for p in (1, 2, 3)]

    assert [[i["id"] for i in page["items"]] for page in pages] == [ids[:2], ids[2:4], ids[4:]]
    assert [page["has_more"] for page in pages] == [True, True, False]
    assert {page["total"] for page in pages} == {5}


@pytest.mark.parametrize("params", [{"size": 0}, {"size": 101}, {"page": 0}])
def test_a_page_outside_the_limits_is_refused(shop, client, params):
    response = client.get(f"{BASE}/discrepancies", params=params, headers=shop.admin())

    assert response.status_code == 422


def test_the_list_can_be_limited_to_one_order(shop, client):
    order, payment = _paid(shop)
    _, other = _paid(shop)
    mine = _discrepancy(shop.db, payment)
    _discrepancy(shop.db, other)

    response = client.get(f"{BASE}/discrepancies", params={"order_id": order["id"]},
                          headers=shop.admin())

    assert [i["id"] for i in response.json()["data"]["items"]] == [mine.id]


# -- a run on demand --------------------------------------------------------------------

def test_a_run_for_one_order_happens_now_and_reports_what_it_found(shop, client):
    order, payment = _paid(shop)
    payment.amount = 2600
    shop.db.commit()
    headers = shop.admin()

    response = client.post(f"{BASE}/run", params={"order_id": order["id"]}, headers=headers)

    assert response.status_code == 200
    data = response.json()["data"]
    assert (data["checked"], data["opened"], data["errors"]) == (1, 1, 0)
    listed = client.get(f"{BASE}/discrepancies", headers=headers).json()["data"]
    assert [(i["kind"], i["ours"], i["theirs"]) for i in listed["items"]] == [
        ("amount", "2600", "2500")]

    # Again: nothing new; and once the data is right it is resolved.
    again = client.post(f"{BASE}/run", params={"order_id": order["id"]}, headers=headers)
    assert again.json()["data"]["opened"] == 0
    shop.db.expire_all()
    payment.amount = 2500
    shop.db.commit()
    fixed = client.post(f"{BASE}/run", params={"order_id": order["id"]}, headers=headers)
    assert fixed.json()["data"]["resolved"] == 1
    assert client.get(f"{BASE}/discrepancies", headers=headers).json()["data"]["total"] == 0


def test_a_run_for_an_unknown_order_is_404(shop, client):
    response = client.post(f"{BASE}/run", params={"order_id": 999}, headers=shop.admin())

    assert response.status_code == 404


def test_a_full_run_is_queued_for_the_worker(shop, client, monkeypatch):
    queued = []
    monkeypatch.setattr(admin.check_payment_reconciliation, "delay",
                        lambda *a, **k: queued.append((a, k)))

    response = client.post(f"{BASE}/run", headers=shop.admin())

    assert response.status_code == 202
    assert response.json()["data"] == {"queued": True}
    assert queued == [((), {})]
