"""How many guest carts one client address can open (F-20, F-39).

Every POST /cart/items without a guest cart cookie (or with one naming no
stored cart) stores a new guest_cart:* key that lives for
REDIS_GUEST_CART_TTL_DAYS. A client that never sends the cookie back used
to be held back only by RATE_LIMIT_DEFAULT, so one address could leave
hundreds of thousands of keys behind per day. RATE_LIMIT_GUEST_CART caps
the carts opened per address; adding to a cart that exists is not counted.
"""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import Settings, settings
from app.main import app
from app.models.category import Category
from app.models.product import Product
from app.schemas.cart import GUEST_CART_KEY_PREFIX

CART = f"{settings.API_V1_STR}/cart"
UNLIMITED = "1000000/minute"


@pytest.fixture
def limits(monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    for name in ("DEFAULT", "SEARCH", "AUTH", "CHECKOUT", "LOGIN_FAILURES"):
        monkeypatch.setattr(settings, f"RATE_LIMIT_{name}", UNLIMITED)
    monkeypatch.setattr(settings, "RATE_LIMIT_GUEST_CART", "2/hour")


@pytest.fixture
def product(db_session):
    category = Category(name="Electronics", level=0, path=[])
    db_session.add(category)
    db_session.commit()
    product = Product(
        name="Widget",
        description="A fine widget for all your widget needs",
        price="9.99",
        stock_quantity=100,
        images=["https://example.com/widget.png"],
        characteristics={},
        category_id=category.id,
    )
    db_session.add(product)
    db_session.commit()
    return product


def _visitor(address="203.0.113.7"):
    # A fresh cookie jar each time: a client that never returns the cookie.
    return TestClient(app, base_url="https://testserver", client=(address, 50000))


def _add(visitor, product_id, headers=None):
    return visitor.post(f"{CART}/items", json={"product_id": product_id}, headers=headers)


def _guest_keys(fake_redis):
    return list(fake_redis.scan_iter(f"{GUEST_CART_KEY_PREFIX}*"))


def test_an_address_can_open_only_so_many_guest_carts(client, limits, product, fake_redis):
    assert _add(_visitor(), product.id).status_code == 200
    assert _add(_visitor(), product.id).status_code == 200

    refused = _add(_visitor(), product.id)

    assert refused.status_code == 429
    assert int(refused.headers["Retry-After"]) > 0
    # Refused before anything is stored.
    assert len(_guest_keys(fake_redis)) == 2


def test_adding_to_an_existing_guest_cart_is_not_counted(client, limits, product, fake_redis):
    guest = _visitor()
    for _ in range(5):
        assert _add(guest, product.id).status_code == 200
    assert len(_guest_keys(fake_redis)) == 1
    assert _add(_visitor(), product.id).status_code == 200


def test_a_cookie_naming_no_stored_cart_counts_as_a_new_cart(client, limits, product, fake_redis):
    # The server replaces an unknown identifier with a new cart, so a made-up
    # cookie must not be a way around the cap.
    made_up = "a" * 43
    for expected in (200, 200, 429):
        visitor = _visitor()
        visitor.cookies.set("__Host-guest_cart", made_up, domain="testserver")
        assert _add(visitor, product.id).status_code == expected


def test_each_address_has_its_own_budget(client, limits, product):
    for _ in range(2):
        assert _add(_visitor("203.0.113.7"), product.id).status_code == 200
    assert _add(_visitor("203.0.113.7"), product.id).status_code == 429
    assert _add(_visitor("198.51.100.9"), product.id).status_code == 200


def test_account_carts_are_not_counted(client, limits, product, valid_registration_payload):
    assert client.post(f"{settings.API_V1_STR}/users/register", json=valid_registration_payload).status_code == 200
    token = client.post(
        f"{settings.API_V1_STR}/users/login",
        data={"username": valid_registration_payload["email"], "password": valid_registration_payload["password"]},
    ).json()["data"]["access_token"]
    for _ in range(3):
        assert _add(_visitor(), product.id, headers={"Authorization": f"Bearer {token}"}).status_code == 200


def test_no_cap_with_the_limiter_switched_off(client, product, monkeypatch):
    monkeypatch.setattr(settings, "RATE_LIMIT_GUEST_CART", "1/hour")
    assert settings.RATE_LIMIT_ENABLED is False
    for _ in range(3):
        assert _add(_visitor(), product.id).status_code == 200


def test_the_setting_is_a_rate():
    assert Settings(_env_file=None).RATE_LIMIT_GUEST_CART
    with pytest.raises(ValidationError):
        Settings(_env_file=None, RATE_LIMIT_GUEST_CART="lots")
