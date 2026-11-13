"""Regression guards for security properties of the API.

Each test asserts the SECURE behaviour and stays as a guard: a client cannot
choose the charge currency, a password reset token works once, deletion of an
account needs the password, the failed-login counter fails closed, erasure
leaves no personal data behind, admin routes need an admin, and one mailbox is
one account whatever the letter case.

The stale checkout session expiry (a late event un-paying a paid order) is
pinned in test_checkout_session.py, next to the payment flow it needs.
"""
import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import TestingSessionLocal
from fastapi import HTTPException
from redis.exceptions import RedisError
from sqlalchemy import func
from starlette.requests import Request

from app.core.config import settings
from app.core.security import generate_password_reset_token
from app.models.address import Address
from app.models.category import Category
from app.models.order import Order
from app.models.product import Product
from app.models.user import User
from app.services.payment.stripe_provider import TOKEN_SUCCESS_DEFAULT, StripePaymentProvider

USERS = "/api/v1/users"
CART = "/api/v1/cart"
ORDERS = "/api/v1/orders"
PASSWORD = "Str0ngPass1"


@pytest.fixture(autouse=True)
def _quiet_side_effects(monkeypatch):
    StripePaymentProvider.reset_mock_state()
    monkeypatch.setattr("app.api.v1.orders.create.send_order_confirmation_email", lambda *a, **k: True)
    yield
    StripePaymentProvider.reset_mock_state()


@pytest.fixture
def db():
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()


def _register_and_login(client, email):
    client.post(f"{USERS}/register", json={
        "email": email, "password": PASSWORD, "full_name": "Test User",
        "gdpr_consent": True, "privacy_policy_accepted": True,
    })
    login = client.post(f"{USERS}/login", data={"username": email, "password": PASSWORD})
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _product(db, price="100.00", stock=10):
    category = Category(name=f"c-{uuid.uuid4().hex[:6]}", path=[], level=0)
    db.add(category)
    db.commit()
    product = Product(name="Widget", description="A widget for tests", price=Decimal(price),
                      stock_quantity=stock, category_id=category.id, images=["https://x.test/a.png"])
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _buyer_with_order(client, db, email="buyer@example.com", price="100.00"):
    headers = _register_and_login(client, email)
    user = db.query(User).filter(User.email == email).first()
    user.is_email_verified = True
    user.phone = "+15551234567"
    address = Address(user_id=user.id, street="1 Main St", city="City", state="ST",
                      postal_code="10001", country="US")
    db.add(address)
    db.commit()
    product = _product(db, price=price)
    added = client.post(f"{CART}/items", json={"product_id": product.id, "quantity": 1}, headers=headers)
    assert added.status_code == 200, added.text
    resp = client.post(ORDERS, json={"shipping_address_id": address.id},
                       headers={**headers, "Idempotency-Key": uuid.uuid4().hex})
    assert resp.status_code == 201, resp.text
    return headers, resp.json(), user


def _order(db, order_id):
    db.expire_all()
    return db.query(Order).filter(Order.id == order_id).first()


# The client must not pick the currency: only the order's own currency is charged.
def test_payment_currency_cannot_be_chosen_by_client(client, db):
    headers, order, _ = _buyer_with_order(client, db)
    resp = client.post(f"{ORDERS}/{order['id']}/pay", headers={**headers, "Idempotency-Key": uuid.uuid4().hex},
                       json={"amount": order["total_amount"], "currency": "JPY",
                             "payment_method_token": TOKEN_SUCCESS_DEFAULT})
    assert resp.status_code == 400, f"order paid in a client-chosen currency: {resp.status_code} {resp.text}"
    assert _order(db, order["id"]).payment_status != "paid"


# A password reset token stops working once it has been used.
def test_password_reset_token_is_single_use(client, db):
    email = "reset@example.com"
    _register_and_login(client, email)
    current_hash = db.query(User).filter(User.email == email).first().hashed_password
    body = {"token": generate_password_reset_token(email, current_hash), "new_password": "NewPass123"}
    first = client.post(f"{USERS}/password/reset/confirm", json=body)
    assert first.status_code == 200, first.text
    second = client.post(f"{USERS}/password/reset/confirm", json={**body, "new_password": "Other4567"})
    assert second.status_code == 400, f"used reset token accepted again: {second.status_code}"


# Erasing an account needs the account's password, not just a bearer token.
def test_legal_deletion_request_requires_reauthentication(client, db):
    headers = _register_and_login(client, "erase@example.com")
    url = "/api/v1/legal/data-request"
    without_password = client.post(url, headers=headers, json={"request_type": "deletion"})
    wrong_password = client.post(url, headers=headers, json={"request_type": "deletion", "password": "WrongPass999"})
    db.expire_all()
    user = db.query(User).filter(User.email == "erase@example.com").first()
    assert without_password.status_code == 422, without_password.text
    assert wrong_password.status_code == 401, wrong_password.text
    assert user.is_active, "account scheduled for erasure without the password"


# When Redis is unreachable the failed-login counter refuses (503); it must not let every attempt through.
def test_login_attempt_counter_does_not_fail_open(monkeypatch):
    from app.core import rate_limit
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)

    def broken():
        raise RedisError("down")
    monkeypatch.setattr(rate_limit, "get_async_redis", broken)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": [], "client": ("203.0.113.9", 1)})

    async def attempts():
        for _ in range(50):
            await rate_limit.check_login_attempt("victim@example.com", request)

    with pytest.raises(HTTPException) as refused:
        asyncio.run(attempts())
    assert refused.value.status_code == 503


# Erasure scrubs soft-deleted addresses and the IP addresses kept in consent_history.
def test_erasure_scrubs_inactive_addresses_and_consent_ips(db, tasks_db):
    from app.tasks import purge_expired_deletion_requests
    thirty_days_ago = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
    user = User(email="gone@example.com", hashed_password="x", full_name="Gone", is_active=False,
                is_email_verified=True, gdpr_consent=True, privacy_policy_accepted=True,
                data_deletion_requested=True, data_deletion_date=thirty_days_ago,
                consent_history=[{"type": "gdpr", "granted": True, "timestamp": "2026-01-01T00:00:00",
                                  "ip_address": "203.0.113.7", "user_agent": "UA"}])
    db.add(user)
    db.commit()
    db.add(Address(user_id=user.id, street="Old Street 5", city="Town", state="ST",
                   postal_code="99999", country="US", is_active=False))
    db.commit()

    purge_expired_deletion_requests(db)
    db.expire_all()
    leftover = db.query(Address).filter(Address.user_id == user.id).all()
    assert all(a.street == "REDACTED" for a in leftover), "inactive address kept its street"
    assert "203.0.113.7" not in json.dumps(db.get(User, user.id).consent_history), "consent IP kept"


# The admin variant listing (stock, SKUs, inactive rows) is not served without authentication.
def test_admin_variant_listing_requires_admin(client, db):
    product = _product(db)
    resp = client.get(f"/api/v1/admin/products/{product.id}/variants")
    assert resp.status_code in (401, 403), f"unauthenticated admin route answered {resp.status_code}"


# Emails that differ only in letter case are one mailbox: one account, and the same answer as a new one.
def test_email_uniqueness_is_case_insensitive(client, db):
    base = {"password": PASSWORD, "full_name": "Case Test", "gdpr_consent": True,
            "privacy_policy_accepted": True}
    first = client.post(f"{USERS}/register", json={**base, "email": "case@example.com"})
    assert first.status_code == 202
    second = client.post(f"{USERS}/register", json={**base, "email": "Case@Example.com"})
    assert second.status_code == first.status_code
    assert db.query(User).filter(func.lower(User.email) == "case@example.com").count() == 1
