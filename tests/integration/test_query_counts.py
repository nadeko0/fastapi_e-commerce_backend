"""The list endpoints issue a bounded number of SQL statements.

A serializer that touches a relationship the query did not load costs one
statement per row (an N+1). Under asyncio it usually fails outright
(MissingGreenlet), but a list that loads through a per-row query of its own
would pass and slow down with the page. Each test fills the list with 1 item,
counts the statements one request issues, grows it to 20 items and counts
again: the two counts must be equal.

The counter listens on the engine the application's async session uses
(AsyncEngine.sync_engine, https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html),
event before_cursor_execute, and counts every statement of the request,
authentication included.
"""

import pytest
from sqlalchemy import event
from test_admin_orders import _admin_headers, _customer, _headers, _order, _product

import app.core.database as database_module
from app.models.category import Category
from app.models.payment import Payment
from app.models.payment_ledger import PaymentDispute, PaymentRefund
from app.models.product import ProductVariant

API = "/api/v1"
SMALL, LARGE = 1, 20
# Not a pin on the exact number (an added eager load may change it); a
# ceiling that tells "a handful" from "one per row".
CEILING = 12


class StatementCounter:
    def __init__(self):
        self.statements = []

    def _record(self, conn, cursor, statement, parameters, context, executemany):
        self.statements.append(statement)

    def __enter__(self):
        self._engine = database_module.AsyncSessionLocal.kw["bind"].sync_engine
        event.listen(self._engine, "before_cursor_execute", self._record)
        return self

    def __exit__(self, *exc):
        event.remove(self._engine, "before_cursor_execute", self._record)


def _count(call):
    """(statements, number of items in the answer) of one request."""
    with StatementCounter() as counter:
        response = call()
    assert response.status_code == 200, response.text
    return len(counter.statements), len(response.json()["items"])


def _grows_with_items(add_item, call):
    add_item(0)
    small, small_items = _count(call)
    for n in range(1, LARGE):
        add_item(n)
    large, large_items = _count(call)
    assert (small_items, large_items) == (SMALL, LARGE)
    return small, large


def _assert_bounded(small, large):
    assert large == small, f"{small} statements for {SMALL} item, {large} for {LARGE}: the query count grows with the page"
    assert 0 < small <= CEILING


def test_product_list(client, db_session):
    def add(n):
        product = _product(db_session, name=f"Widget {n}")
        product.images = ["https://example.com/widget.png"]  # the public list refuses a product without one
        db_session.commit()

    _assert_bounded(*_grows_with_items(add, lambda: client.get(f"{API}/products", params={"size": 50})))


def test_category_list(client, db_session):
    def add(n):
        db_session.add(Category(name=f"Shelf {n}", level=0, path=[]))
        db_session.commit()

    _assert_bounded(*_grows_with_items(add, lambda: client.get(f"{API}/categories", params={"size": 50})))


def _orders_with_items(db_session, customer):
    def add(n):
        _order(
            db_session, customer, total="20.00",
            lines=[(_product(db_session, name=f"Line {n}a"), 1, "10.00"), (_product(db_session, name=f"Line {n}b"), 1, "10.00")],
        )

    return add


def test_my_order_list(client, db_session):
    customer = _customer(db_session, "mine@example.com")
    headers = _headers(client, "mine@example.com")

    _assert_bounded(*_grows_with_items(
        _orders_with_items(db_session, customer),
        lambda: client.get(f"{API}/users/orders", params={"size": 50}, headers=headers),
    ))


def test_admin_order_list(client, db_session):
    customer = _customer(db_session)
    headers = _admin_headers(client, db_session)

    _assert_bounded(*_grows_with_items(
        _orders_with_items(db_session, customer),
        lambda: client.get(f"{API}/admin/orders", params={"size": 50}, headers=headers),
    ))


def test_admin_variant_list(client, db_session):
    product = _product(db_session)
    headers = _admin_headers(client, db_session)

    def add(n):
        db_session.add(ProductVariant(product_id=product.id, sku=f"SKU-{n}", attributes={"n": n}, stock_quantity=1))
        db_session.commit()

    _assert_bounded(*_grows_with_items(
        add, lambda: client.get(f"{API}/admin/products/{product.id}/variants", headers=headers)
    ))


def test_order_payment_list_with_refunds_and_disputes(client, db_session):
    order = _order(db_session, _customer(db_session), payment_status="paid")
    headers = _admin_headers(client, db_session)

    def add(n):
        payment = Payment(
            order_id=order.id, payment_intent_id=f"pi_{n}", amount=1000, currency="usd", status="succeeded",
        )
        db_session.add(payment)
        db_session.commit()
        db_session.add(PaymentRefund(
            payment_id=payment.id, refund_id=f"re_{n}", amount=100, currency="USD", status="succeeded", source="admin",
        ))
        db_session.add(PaymentDispute(
            payment_id=payment.id, dispute_id=f"dp_{n}", amount=100, currency="USD", status="needs_response",
        ))
        db_session.commit()

    _assert_bounded(*_grows_with_items(add, lambda: client.get(f"{API}/orders/{order.id}/payments", headers=headers)))


def test_the_guard_catches_a_list_that_queries_once_per_row(db_session):
    # The guard itself, on a route written with the N+1 on purpose: one
    # query for the categories, then one more for each of them.
    from typing import Any

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import func, select

    from app.core.database import DbSession

    probe = FastAPI()

    @probe.get("/categories")
    async def categories(db: DbSession) -> Any:
        rows = (await db.execute(select(Category))).scalars().all()
        for row in rows:
            await db.scalar(select(func.count()).select_from(Category).where(Category.parent_id == row.id))
        return {"items": [row.id for row in rows]}

    def add(n):
        db_session.add(Category(name=f"Shelf {n}", level=0, path=[]))
        db_session.commit()

    with TestClient(probe) as client:
        small, large = _grows_with_items(add, lambda: client.get("/categories"))

    assert (small, large) == (2, 21)
    with pytest.raises(AssertionError, match="grows with the page"):
        _assert_bounded(small, large)
