"""Every response that carries an amount also names its currency (ISO 4217),
as a sibling field of the amounts (Zalando guideline 173, "money object").
Catalog, cart and statistics amounts are in settings.STORE_CURRENCY; an order
carries its own currency (Order.currency), which its items share."""

import json

import pytest
from payment_support import Shop

from app.core.config import settings

API = "/api/v1"


@pytest.fixture
def shop(client, db_session, monkeypatch):
    # Not the default, so a hard-coded "USD" cannot pass.
    monkeypatch.setattr(settings, "STORE_CURRENCY", "EUR")
    shop = Shop(client, db_session)
    shop.product_row = shop.product()
    shop.product_row.description = "A fine widget for all your widget needs"
    db_session.commit()
    shop.variant_row = shop.variant(shop.product_row, price_override="12.50")
    return shop


def test_product_and_listing_and_search_carry_currency(shop):
    pid = shop.product_row.id
    one = shop.client.get(f"{API}/products/{pid}").json()["data"]
    assert one["currency"] == "EUR"
    listing = shop.client.get(f"{API}/products").json()["data"]["items"]
    assert listing and all(p["currency"] == "EUR" for p in listing)
    found = shop.client.get(f"{API}/products/search", params={"query": "Widget"})
    assert found.status_code == 200
    assert all(p["currency"] == "EUR" for p in found.json()["data"]["items"])


def test_old_cached_product_body_is_served_with_currency(shop, fake_redis):
    pid = shop.product_row.id
    first = shop.client.get(f"{API}/products/{pid}").json()["data"]
    cached = json.loads(fake_redis.get(f"product:{pid}"))
    cached.pop("currency", None)  # written by a release before currency existed
    fake_redis.set(f"product:{pid}", json.dumps(cached))
    again = shop.client.get(f"{API}/products/{pid}").json()["data"]
    assert again["currency"] == "EUR"
    assert again["price"] == first["price"]


def test_variants_carry_currency(shop):
    headers = shop.admin()
    pid = shop.product_row.id
    created = shop.client.post(f"{API}/admin/products/{pid}/variants", headers=headers,
                               json={"sku": "CUR-1", "price_override": "5.00"})
    assert created.json()["data"]["currency"] == "EUR"
    listed = shop.client.get(f"{API}/admin/products/{pid}/variants", headers=headers)
    assert all(v["currency"] == "EUR" for v in listed.json()["data"]["items"])
    vid = shop.variant_row.id
    updated = shop.client.put(f"{API}/admin/products/{pid}/variants/{vid}", headers=headers,
                              json={"price_override": "6.00"})
    assert updated.json()["data"]["currency"] == "EUR"


def test_admin_product_writes_carry_currency(shop):
    headers = shop.admin()
    pid = shop.product_row.id
    body = shop.client.put(f"{API}/admin/products/{pid}", headers=headers,
                           json={"price": "20.00"}).json()["data"]
    assert body["currency"] == "EUR"


def test_cart_lines_and_total_carry_currency(shop):
    buyer = shop.buyer()
    shop.add_to_cart(buyer, shop.product_row, 2)
    cart = shop.client.get(f"{API}/cart", headers=buyer.headers).json()["data"]
    assert cart["currency"] == "EUR"
    assert cart["items"] and all(i["currency"] == "EUR" for i in cart["items"])


def test_cart_price_changes_carry_currency(shop, db_session):
    buyer = shop.buyer()
    shop.add_to_cart(buyer, shop.product_row)
    shop.product_row.price = shop.product_row.price + 1
    db_session.commit()
    result = shop.client.post(f"{API}/cart/validate", headers=buyer.headers)
    changes = result.json()["data"]["price_changes"]
    assert changes and all(c["currency"] == "EUR" for c in changes)


def test_order_and_its_items_sit_under_the_orders_currency(shop):
    buyer = shop.buyer()
    order = shop.order_of(buyer, shop.product_row)
    assert order["currency"] == "EUR"
    assert order["items"]


def test_admin_stats_carry_currency_even_from_an_old_cache(shop, fake_redis):
    headers = shop.admin()
    stats = shop.client.get(f"{API}/admin/stats", headers=headers).json()["data"]
    assert stats["currency"] == "EUR"
    for key in fake_redis.scan_iter("admin_stats_*"):
        old = json.loads(fake_redis.get(key))
        old.pop("currency", None)
        for p in old.get("popular_products", []):
            p.pop("currency", None)
        fake_redis.set(key, json.dumps(old))
    again = shop.client.get(f"{API}/admin/stats", headers=headers).json()["data"]
    assert again["currency"] == "EUR"
