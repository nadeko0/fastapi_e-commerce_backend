"""2B-01 end to end through the HTTP routes on PostgreSQL."""
import threading, time
import pytest
from conftest import engine, async_engine, requires_postgres
from fastapi.testclient import TestClient
from payment_support import Shop
from sqlalchemy import event
from app.main import app

pytestmark = requires_postgres


def _run(actions, hold_thread_index=0, hold_seconds=1.5, start_gap=0.3):
    results, idents, fired = {}, {}, []

    def hold(connection, cursor, statement, *rest):
        if statement.startswith("UPDATE product_variants SET stock_quantity") and not fired:
            fired.append(1)
            time.sleep(hold_seconds)

    def run(i):
        idents[i] = threading.get_ident()
        with TestClient(app) as c:
            results[i] = actions[i](c)

    event.listen(async_engine.sync_engine, "after_cursor_execute", hold)
    try:
        ts = [threading.Thread(target=run, args=(i,)) for i in range(len(actions))]
        ts[0].start(); time.sleep(start_gap); ts[1].start()
        for t in ts: t.join(timeout=60)
    finally:
        event.remove(async_engine.sync_engine, "after_cursor_execute", hold)
    return results


def test_variant_checkout_vs_plain_plus_variant_checkout(client, db_session):
    shop = Shop(client, db_session)
    p = shop.product(stock=5, name="P")
    v = shop.variant(p, stock=5)
    a, b = shop.buyer(), shop.buyer()
    assert shop.add_to_cart(a, p, variant=v).status_code == 200            # A: [V]
    assert shop.add_to_cart(b, p).status_code == 200                       # B: [P plain,
    assert shop.add_to_cart(b, p, variant=v).status_code == 200            #     V]
    results = _run([lambda c: Shop(c, db_session).place_order(a),
                    lambda c: Shop(c, db_session).place_order(b)])
    codes = sorted(r.status_code for r in results.values())
    print({i: (r.status_code, r.text[:120]) for i, r in results.items()})
    assert codes == [201, 201], codes


def test_variant_checkout_vs_cancel_of_plain_plus_variant_order(client, db_session):
    shop = Shop(client, db_session)
    p = shop.product(stock=5, name="P")
    v = shop.variant(p, stock=5)
    seller, buyer = shop.buyer(), shop.buyer()
    shop.add_to_cart(seller, p); shop.add_to_cart(seller, p, variant=v)
    order_id = shop.place_order(seller).json()["id"]
    shop.add_to_cart(buyer, p, variant=v)
    results = _run([lambda c: Shop(c, db_session).place_order(buyer),
                    lambda c: Shop(c, db_session).cancel(seller, order_id)])
    print({i: (r.status_code, r.text[:120]) for i, r in results.items()})
    assert sorted(r.status_code for r in results.values()) == [200, 201]


def test_existing_harness_hook_fires(client, db_session):
    """Does the hook used by test_postgres_only._run_holding_after_first_stock_update
    (listening on the SYNC engine) ever see the checkout's stock UPDATE?"""
    seen = []
    def spy(connection, cursor, statement, *rest):
        if statement.startswith("UPDATE products SET stock_quantity"):
            seen.append(statement)
    shop = Shop(client, db_session)
    p = shop.product(stock=5, name="Q")
    b = shop.buyer()
    shop.add_to_cart(b, p)
    event.listen(engine, "after_cursor_execute", spy)
    try:
        assert shop.place_order(b).status_code == 201
    finally:
        event.remove(engine, "after_cursor_execute", spy)
    print("sync-engine hook saw", len(seen), "stock UPDATEs")
