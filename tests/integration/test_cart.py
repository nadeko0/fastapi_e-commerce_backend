import threading

from app.models.category import Category
from app.models.product import Product, ProductVariant

USERS_PREFIX = "/api/v1/users"
CART_PREFIX = "/api/v1/cart"


def _valid_registration_payload():
    return {
        "email": "cart.user@example.com",
        "password": "Str0ngPass1",
        "full_name": "Cart User",
        "gdpr_consent": True,
        "privacy_policy_accepted": True,
        "marketing_consent": False,
    }


def _register_and_login(client, payload):
    client.post(f"{USERS_PREFIX}/register", json=payload)
    login_response = client.post(
        f"{USERS_PREFIX}/login",
        data={"username": payload["email"], "password": payload["password"]},
    )
    return login_response.json()["data"]["access_token"]


def _auth_headers(token):
    return {"Authorization": f"Bearer {token}"}


def _make_category(db_session):
    category = Category(name="Electronics", parent_id=None, level=0, path=[])
    db_session.add(category)
    db_session.commit()
    db_session.refresh(category)
    return category


def _make_product(db_session, category_id, stock=10, price="9.99"):
    product = Product(
        name="Widget",
        description="A fine widget for all your widget needs",
        price=price,
        stock_quantity=stock,
        images=["https://example.com/widget.png"],
        characteristics={},
        category_id=category_id,
    )
    db_session.add(product)
    db_session.commit()
    db_session.refresh(product)
    return product


def _make_variant(db_session, product_id, sku="WIDGET-M-RED", stock=5, price_override=None, is_active=True):
    variant = ProductVariant(
        product_id=product_id,
        sku=sku,
        attributes={"size": "M", "color": "red"},
        price_override=price_override,
        stock_quantity=stock,
        is_active=is_active,
    )
    db_session.add(variant)
    db_session.commit()
    db_session.refresh(variant)
    return variant


def test_get_cart_starts_empty(client, valid_registration_payload):
    token = _register_and_login(client, valid_registration_payload)

    response = client.get(CART_PREFIX, headers=_auth_headers(token))

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items"] == []
    assert body["items_count"] == 0


def test_add_item_to_cart(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 1
    assert body["items"][0]["quantity"] == 2


def test_add_item_rejects_unknown_product(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": 999999, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_add_item_rejects_quantity_over_stock(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=2)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    assert response.status_code == 400


def test_update_cart_item_quantity(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.put(
        f"{CART_PREFIX}/items/{product.id}",
        params={"quantity": 5},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["quantity"] == 5


def test_update_cart_item_rejects_quantity_over_stock(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=3)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.put(
        f"{CART_PREFIX}/items/{product.id}",
        params={"quantity": 10},
        headers=_auth_headers(token),
    )

    assert response.status_code == 400


def test_update_item_not_in_cart_returns_404(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    response = client.put(
        f"{CART_PREFIX}/items/{product.id}",
        params={"quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_remove_item_from_cart(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.delete(
        f"{CART_PREFIX}/items/{product.id}", headers=_auth_headers(token)
    )

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


def test_remove_item_not_in_cart_returns_404(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())

    response = client.delete(f"{CART_PREFIX}/items/999999", headers=_auth_headers(token))

    assert response.status_code == 404


def test_clear_cart(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.delete(CART_PREFIX, headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"]["items"] == []


def test_validate_cart_reports_no_issues_when_stock_sufficient(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    response = client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token))

    assert response.status_code == 200
    assert response.json()["data"]["valid"] is True
    assert response.json()["data"]["issues"] == []


def test_validate_cart_reports_issue_when_stock_reduced_after_adding(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=5)
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    # Stock drops below what's in the cart after the item was added.
    db_session.query(Product).filter(Product.id == product.id).update(
        {"stock_quantity": 1}
    )
    db_session.commit()

    response = client.post(f"{CART_PREFIX}/validate", headers=_auth_headers(token))

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["valid"] is False
    assert body["issues"][0]["product_id"] == product.id


def test_cart_requires_authentication(client):
    response = client.get(CART_PREFIX)

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Concurrency: rapid add-to-cart for the same product
# ---------------------------------------------------------------------------

def test_sequential_add_to_cart_accumulates_quantity(client, db_session):
    """Two add-to-cart calls for the same product, one after another,
    must accumulate (2 + 3 = 5), not overwrite. This proves add_to_cart's
    read-modify-write logic is correct when calls do not overlap - it does
    NOT by itself prove safety under genuine concurrent requests; see
    test_concurrent_add_to_cart_can_lose_updates below for that."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 1
    assert body["items"][0]["quantity"] == 5


def test_concurrent_add_to_cart_can_lose_updates(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100)

    n = 10
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 200 for code in results)

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    final_quantity = final["items"][0]["quantity"] if final["items"] else 0

    # Desired behavior: all 10 concurrent +1 additions accumulate to 10.
    # Currently fails: last writer wins, earlier additions are lost.
    assert final_quantity == n


def test_high_concurrency_add_to_cart_sums_exactly(client, db_session):
    """Same lost-update scenario as above but at higher concurrency (40
    threads) to make sure the WATCH/MULTI retry loop in
    RedisService._atomic_mutate_cart holds up under heavier contention,
    not just enough to beat a handful of threads."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=1000)

    n = 40
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 200 for code in results)

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    final_quantity = final["items"][0]["quantity"] if final["items"] else 0
    assert final_quantity == n


def test_concurrent_add_different_products_no_cross_clobbering(client, db_session):
    """Concurrent adds of two different products to the same cart must
    both land with correct quantities - the WATCH/MULTI retry loop must
    not let one product's write stomp the other's."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product_a = _make_product(db_session, category.id, stock=100)
    product_b = _make_product(db_session, category.id, stock=100)

    n = 10
    barrier = threading.Barrier(n * 2)
    results = [None] * (n * 2)

    def worker(i, product_id):
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product_id, "quantity": 1},
            headers=_auth_headers(token),
        )
        results[i] = response.status_code

    threads = []
    for i in range(n):
        threads.append(threading.Thread(target=worker, args=(i, product_a.id)))
    for i in range(n):
        threads.append(threading.Thread(target=worker, args=(n + i, product_b.id)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 200 for code in results)

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    quantities = {item["product_id"]: item["quantity"] for item in final["items"]}
    assert quantities == {product_a.id: n, product_b.id: n}


def test_concurrent_add_and_remove_same_item_is_consistent(client, db_session):
    """A concurrent add and remove of the same cart item must not crash
    (e.g. KeyError from a mutator running against a stale copy) and must
    leave the cart in a valid state: either the item is gone, or it's
    present with a positive quantity - never a phantom zero-quantity
    entry or an unhandled exception."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 5},
        headers=_auth_headers(token),
    )

    n = 8
    barrier = threading.Barrier(n)
    results = [None] * n

    def add_worker(i):
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        results[i] = response.status_code

    def remove_worker(i):
        barrier.wait()
        response = client.delete(
            f"{CART_PREFIX}/items/{product.id}", headers=_auth_headers(token)
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=add_worker, args=(i,)) for i in range(n // 2)]
    threads += [
        threading.Thread(target=remove_worker, args=(n // 2 + i,))
        for i in range(n // 2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every add succeeds (200); a remove either succeeds (200, it was
    # first) or 404s (the item was already gone) - never anything else.
    assert all(code in (200, 404) for code in results)

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    if final["items"]:
        assert len(final["items"]) == 1
        assert final["items"][0]["product_id"] == product.id
        assert final["items"][0]["quantity"] > 0


def test_concurrent_add_and_quantity_update_same_item(client, db_session):
    """A concurrent add (+3) and a quantity-update (set to 10) of the same
    item must serialize through the WATCH/MULTI guard into one of the two
    valid outcomes for their real execution order - never a lost update
    (e.g. collapsing back to the pre-existing quantity of 5) and never a
    crash."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 5},
        headers=_auth_headers(token),
    )

    barrier = threading.Barrier(2)
    results = [None, None]

    def add_worker():
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 3},
            headers=_auth_headers(token),
        )
        results[0] = response.status_code

    def update_worker():
        barrier.wait()
        response = client.put(
            f"{CART_PREFIX}/items/{product.id}",
            params={"quantity": 10},
            headers=_auth_headers(token),
        )
        results[1] = response.status_code

    threads = [threading.Thread(target=add_worker), threading.Thread(target=update_worker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code == 200 for code in results)

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    final_quantity = final["items"][0]["quantity"]
    # update-wins-last -> 10; add-wins-last (applied on top of the update's
    # 10) -> 13. Either is a valid serialization; 5 (update lost) or 8
    # (add lost, update never applied) would mean an update was dropped.
    assert final_quantity in (10, 13)


def test_stock_limit_enforced_under_concurrent_adds(client, db_session):
    """20 concurrent +1 adds against a product with only 10 units of
    stock must not collectively oversell it: exactly 10 requests succeed
    and the rest are rejected with 400, because the stock check inside
    the atomic mutator always runs against the freshest cart quantity on
    every WATCH retry."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10)

    n = 20
    barrier = threading.Barrier(n)
    results = [None] * n

    def worker(i):
        barrier.wait()
        response = client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )
        results[i] = response.status_code

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(code in (200, 400) for code in results)
    succeeded = sum(1 for code in results if code == 200)
    assert succeeded == 10

    final = client.get(CART_PREFIX, headers=_auth_headers(token)).json()["data"]
    final_quantity = final["items"][0]["quantity"] if final["items"] else 0
    assert final_quantity == 10


def test_cart_ttl_refreshed_after_concurrent_writes(client, db_session, fake_redis):
    """The cart key's TTL must still be (re)set correctly after the
    WATCH/MULTI-guarded concurrent writes, matching the existing
    CART_TTL_DAYS behavior of the old plain SETEX path."""
    from app.schemas.cart import CART_KEY_PREFIX, CART_TTL_DAYS

    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100)

    n = 10
    barrier = threading.Barrier(n)

    def worker():
        barrier.wait()
        client.post(
            f"{CART_PREFIX}/items",
            params={"product_id": product.id, "quantity": 1},
            headers=_auth_headers(token),
        )

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Find the user id from the cart key namespace by scanning - there's
    # only one cart in this test's fake Redis instance.
    cart_keys = list(fake_redis.scan_iter(f"{CART_KEY_PREFIX}*"))
    assert len(cart_keys) == 1

    ttl_seconds = fake_redis.ttl(cart_keys[0])
    assert ttl_seconds > 0
    assert ttl_seconds <= CART_TTL_DAYS * 24 * 3600


# ---------------------------------------------------------------------------
# Product variants in the cart
# ---------------------------------------------------------------------------

def test_add_variant_to_cart_prices_and_stocks_against_variant(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=3, price_override="14.50")

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 1
    item = body["items"][0]
    assert item["product_id"] == product.id
    assert item["variant_id"] == variant.id
    assert item["quantity"] == 2
    # Variant's price_override (14.50) is used, not the product's base price.
    assert item["price_at_time"] == 14.50


def test_add_variant_without_price_override_uses_product_price(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=3, price_override=None)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    assert response.json()["data"]["items"][0]["price_at_time"] == 9.99


def test_add_variant_rejects_quantity_over_variant_stock(client, db_session):
    """The variant's own stock (3) is the limit, even though the parent
    product has plenty of stock (100) - checking the wrong counter would
    let this through."""
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=100, price="9.99")
    variant = _make_variant(db_session, product.id, stock=3)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 4},
        headers=_auth_headers(token),
    )

    assert response.status_code == 400


def test_add_variant_rejects_id_belonging_to_different_product(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product_a = _make_product(db_session, category.id)
    product_b = _make_product(db_session, category.id)
    variant_of_b = _make_variant(db_session, product_b.id, sku="B-VARIANT")

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product_a.id, "variant_id": variant_of_b.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_add_variant_rejects_inactive_variant(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)
    variant = _make_variant(db_session, product.id, is_active=False)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_add_variant_rejects_unknown_variant_id(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id)

    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": 999999, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 404


def test_different_variants_of_same_product_are_distinct_cart_lines(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant_red = _make_variant(db_session, product.id, sku="WIDGET-RED", stock=5)
    variant_blue = _make_variant(db_session, product.id, sku="WIDGET-BLUE", stock=5)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant_red.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant_blue.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    # And the plain product itself (no variant) is a third, separate line.
    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 3
    variant_ids = {item["variant_id"] for item in body["items"]}
    assert variant_ids == {variant_red.id, variant_blue.id, None}


def test_same_variant_added_twice_merges_quantity(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=10)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 2},
        headers=_auth_headers(token),
    )
    response = client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 3},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 1
    assert body["items"][0]["quantity"] == 5


def test_remove_variant_line_leaves_plain_product_line_untouched(client, db_session):
    token = _register_and_login(client, _valid_registration_payload())
    category = _make_category(db_session)
    product = _make_product(db_session, category.id, stock=10, price="9.99")
    variant = _make_variant(db_session, product.id, stock=5)

    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "quantity": 1},
        headers=_auth_headers(token),
    )
    client.post(
        f"{CART_PREFIX}/items",
        params={"product_id": product.id, "variant_id": variant.id, "quantity": 1},
        headers=_auth_headers(token),
    )

    response = client.delete(
        f"{CART_PREFIX}/items/{product.id}",
        params={"variant_id": variant.id},
        headers=_auth_headers(token),
    )

    assert response.status_code == 200
    body = response.json()["data"]
    assert body["items_count"] == 1
    assert body["items"][0]["variant_id"] is None
