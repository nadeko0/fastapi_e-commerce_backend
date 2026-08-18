"""Smoke test against a real PostgreSQL instance.

Exercises Postgres-specific behavior the SQLite-backed pytest suite
cannot genuinely validate: ARRAY columns, JSONB path filtering, partial
unique indexes, and CheckConstraints. Not part of the pytest suite (no
Postgres is available in CI/most dev sandboxes) - run manually against a
disposable database after `alembic upgrade head`:

    createdb ecommerce_smoketest
    POSTGRES_DB=ecommerce_smoketest uv run alembic upgrade head
    POSTGRES_DB=ecommerce_smoketest uv run python scripts/pg_smoke_test.py

Destructive: TRUNCATEs every table it touches. Point it at a throwaway
database, never at a real one.
"""
import os
import sys
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.database import SessionLocal, engine  # noqa: E402
from app.core.security import get_password_hash  # noqa: E402
from app.models.address import Address  # noqa: E402
from app.models.category import Category  # noqa: E402
from app.models.enums import OrderStatus, PaymentStatus, UserRole  # noqa: E402
from app.models.order import Order  # noqa: E402
from app.models.order_items import OrderItem  # noqa: E402
from app.models.payment import Payment, WebhookEvent  # noqa: E402
from app.models.product import Product  # noqa: E402
from app.models.user import User  # noqa: E402

failures = []


def check(label, cond):
    status = "OK" if cond else "FAIL"
    print(f"[{status}] {label}")
    if not cond:
        failures.append(label)


db = SessionLocal()
try:
    db.execute(text(
        "TRUNCATE TABLE payments, webhook_events, order_items, orders, "
        "addresses, products, categories, users RESTART IDENTITY CASCADE"
    ))
    db.commit()

    # --- ARRAY column round-trip (Product.images) ---
    category = Category(name="Electronics", parent_id=None, level=0, path=[])
    db.add(category)
    db.commit()
    db.refresh(category)

    product = Product(
        name="Real Postgres Widget",
        description="Exercises ARRAY(String) images column on real Postgres",
        price=Decimal("19.99"),
        stock_quantity=10,
        images=["https://example.com/a.png", "https://example.com/b.png"],
        characteristics={"color": {"name": "Color", "value": "red"}},
        category_id=category.id,
        is_active=True,
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    check(
        "ARRAY(String) images round-trips as a Python list",
        product.images == ["https://example.com/a.png", "https://example.com/b.png"],
    )

    other = Product(
        name="Other Widget", description="A different widget entirely",
        price=Decimal("5.00"), stock_quantity=5, images=["https://example.com/c.png"],
        characteristics={"color": {"name": "Color", "value": "blue"}},
        category_id=category.id, is_active=True,
    )
    db.add(other)
    db.commit()

    # --- Nested JSONB path filter, matching app/api/v1/products.py's
    # characteristics filter: Product.characteristics[key]["value"].as_string() ---
    matched = db.query(Product).filter(
        Product.characteristics["color"]["value"].as_string() == "red"
    ).all()
    check(
        "Nested characteristics[key]['value'].as_string() filter matches exactly the right row",
        [p.name for p in matched] == ["Real Postgres Widget"],
    )

    # --- Atomic stock decrement (the race-condition fix in orders.create_order) ---
    updated = db.query(Product).filter(
        Product.id == product.id, Product.stock_quantity >= 10
    ).update({Product.stock_quantity: Product.stock_quantity - 10}, synchronize_session=False)
    db.commit()
    check("Atomic UPDATE...WHERE stock decrement affected exactly 1 row", updated == 1)
    db.refresh(product)
    check("Stock actually decremented to 0", product.stock_quantity == 0)
    updated_again = db.query(Product).filter(
        Product.id == product.id, Product.stock_quantity >= 1
    ).update({Product.stock_quantity: Product.stock_quantity - 1}, synchronize_session=False)
    db.commit()
    check("Atomic decrement correctly refuses oversell (0 rows affected)", updated_again == 0)

    # --- User + Address + partial unique index ---
    user = User(
        email="pgsmoke@example.com",
        hashed_password=get_password_hash("Str0ngPass1"),
        full_name="PG Smoke", phone="+123456789", role=UserRole.CLIENT,
        gdpr_consent=True, privacy_policy_accepted=True, marketing_consent=False,
        consent_history=[],
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    addr1 = Address(
        user_id=user.id, street="123 Real St", city="Realtown", state="RT",
        postal_code="12345", country="EU", is_default=True, is_active=True,
    )
    db.add(addr1)
    db.commit()
    db.refresh(addr1)

    addr_dup = Address(
        user_id=user.id, street="123 Real St", city="Realtown", state="RT",
        postal_code="12345", country="EU", is_default=False, is_active=True,
    )
    db.add(addr_dup)
    dup_rejected = False
    try:
        db.commit()
    except IntegrityError:
        dup_rejected = True
        db.rollback()
    check("Partial unique index rejects duplicate active address for same user", dup_rejected)

    addr_inactive_dup = Address(
        user_id=user.id, street="123 Real St", city="Realtown", state="RT",
        postal_code="12345", country="EU", is_default=False, is_active=False,
    )
    db.add(addr_inactive_dup)
    inactive_dup_ok = True
    try:
        db.commit()
    except IntegrityError:
        inactive_dup_ok = False
        db.rollback()
    check("Partial unique index allows a duplicate when is_active=False", inactive_dup_ok)

    # --- CheckConstraint enforcement ---
    bad_role_rejected = False
    try:
        db.execute(text("UPDATE users SET role = 'not_a_real_role' WHERE id = :id"), {"id": user.id})
        db.commit()
    except IntegrityError:
        bad_role_rejected = True
        db.rollback()
    check("CheckConstraint ck_user_role rejects an invalid role value", bad_role_rejected)

    # --- Order idempotency-key uniqueness ---
    order = Order(
        user_id=user.id, status=OrderStatus.NEW, payment_status=PaymentStatus.PENDING,
        shipping_address_id=addr1.id, idempotency_key="smoke-key-1", total_amount=Decimal("19.99"),
    )
    db.add(order)
    db.commit()
    db.refresh(order)

    order_item = OrderItem(order_id=order.id, product_id=product.id, quantity=1, price_at_time=Decimal("19.99"))
    db.add(order_item)
    db.commit()
    check("OrderItem.product_name property resolves via relationship", order_item.product_name == "Real Postgres Widget")

    order_dup_key = Order(
        user_id=user.id, status=OrderStatus.NEW, payment_status=PaymentStatus.PENDING,
        shipping_address_id=addr1.id, idempotency_key="smoke-key-1", total_amount=Decimal("5.00"),
    )
    db.add(order_dup_key)
    dup_key_rejected = False
    try:
        db.commit()
    except IntegrityError:
        dup_key_rejected = True
        db.rollback()
    check("Partial unique index rejects duplicate (user_id, idempotency_key)", dup_key_rejected)

    # --- Payment + WebhookEvent idempotency guard ---
    payment = Payment(
        order_id=order.id, provider="stripe", payment_intent_id="pi_smoke_1",
        amount=1999, currency="EUR", status="succeeded",
    )
    db.add(payment)
    db.commit()

    webhook = WebhookEvent(event_id="evt_smoke_1", event_type="payment_intent.succeeded")
    db.add(webhook)
    db.commit()
    webhook_dup = WebhookEvent(event_id="evt_smoke_1", event_type="payment_intent.succeeded")
    db.add(webhook_dup)
    webhook_dup_rejected = False
    try:
        db.commit()
    except IntegrityError:
        webhook_dup_rejected = True
        db.rollback()
    check("WebhookEvent.event_id unique constraint enforces webhook idempotency", webhook_dup_rejected)

finally:
    db.close()
    engine.dispose()

print()
if failures:
    print(f"{len(failures)} FAILURE(S): {failures}")
    sys.exit(1)
print("All Postgres-specific smoke checks passed.")
