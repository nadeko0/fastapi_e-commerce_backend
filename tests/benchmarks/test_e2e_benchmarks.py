"""End-to-end benchmarks through the ASGI app using TestClient.

Reuses the test suite's SQLite and fakeredis test fixtures from tests/conftest.py.
Measures GET one product, GET product list with a 20-item page, and authenticated
GET cart retrieval with catalog pricing.
"""
from decimal import Decimal

import pytest

from app.core.security import DUMMY_PASSWORD_HASH, create_access_token
from app.models.category import Category
from app.models.product import Product
from app.models.user import User, UserRole


@pytest.fixture
def seeded_catalog(db_session):
    # Once per test, not once per round: the rounds all run inside the one
    # benchmark() call. The suite's own cleanup empties the tables afterwards.
    db = db_session
    category = Category(name="Benchmark Category", level=0, path=[])
    db.add(category)
    db.commit()
    db.refresh(category)

    products = []
    for i in range(1, 21):
        p = Product(
            name=f"Benchmark Product {i:02d}",
            description=f"High-performance benchmark product line item {i:02d}.",
            price=Decimal(f"{10 + i}.99"),
            stock_quantity=100,
            category_id=category.id,
            images=["https://example.com/images/bench-product.png"],
            characteristics={
                "sku": {"name": "sku", "value": f"BENCH-{i:02d}"},
            },
        )
        products.append(p)
    db.add_all(products)

    user = User(
        email="benchmark.user@example.com",
        hashed_password=DUMMY_PASSWORD_HASH,
        full_name="Benchmark Customer",
        is_active=True,
        is_email_verified=True,
        role=UserRole.CLIENT,
        gdpr_consent=True,
        privacy_policy_accepted=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    for p in products:
        db.refresh(p)

    product_ids = [p.id for p in products]
    user_id = user.id
    token = create_access_token(user_id)
    auth_headers = {"Authorization": f"Bearer {token}"}
    category_id = category.id
    db.close()
    return {
        "category_id": category_id,
        "product_ids": product_ids,
        "user_id": user_id,
        "auth_headers": auth_headers,
    }


def test_bench_e2e_get_one_product(benchmark, client, seeded_catalog):
    # Measures the full synchronous call through TestClient for GET /api/v1/products/{id}.
    # The number includes TestClient request dispatch and event loop handling,
    # FastAPI middleware, routing, dependency injection (AsyncSessionLocal and Cache),
    # async database query execution on SQLite via aiosqlite, ORM model hydration,
    # Pydantic response validation, and JSON serialization.
    product_id = seeded_catalog["product_ids"][0]
    response = benchmark(client.get, f"/api/v1/products/{product_id}")
    assert response.status_code == 200
    assert response.json()["id"] == product_id


def test_bench_e2e_get_product_list(benchmark, client, seeded_catalog):
    # Measures GET /api/v1/products?page=1&size=20 through TestClient.
    # The number includes query parameter parsing and validation via ProductFilter,
    # async query execution for 20 items plus total count on SQLite,
    # Pydantic serialization of ProductListResponse, and HTTP response delivery.
    response = benchmark(client.get, "/api/v1/products?page=1&size=20")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 20
    assert len(body["items"]) == 20


def test_bench_e2e_get_cart_authenticated(benchmark, client, seeded_catalog):
    # Measures authenticated GET /api/v1/cart through TestClient.
    # The number includes Bearer token extraction, JWT signature verification and
    # claims validation, async Redis cart retrieval from fakeredis, catalog price
    # resolution query against SQLite, CartResponse serialization, and response delivery.
    auth_headers = seeded_catalog["auth_headers"]
    product_id = seeded_catalog["product_ids"][0]
    # Seed one item into the user's cart once before the benchmark runs (not once per round).
    add_resp = client.post(
        "/api/v1/cart/items",
        json={"product_id": product_id, "quantity": 2},
        headers=auth_headers,
    )
    assert add_resp.status_code == 200

    response = benchmark(client.get, "/api/v1/cart", headers=auth_headers)
    assert response.status_code == 200
    body = response.json()
    assert body["items_count"] >= 1
    assert Decimal(str(body["total_amount"])) > Decimal("0.00")
