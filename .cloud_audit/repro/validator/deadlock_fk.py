"""Reproduce: FOR UPDATE (lock_stock_rows) vs FK KEY SHARE taken by the
order_items INSERT -> deadlock between two checkouts / checkout vs cancel."""
import os, sys, threading, time
from decimal import Decimal
for k, v in {"JWT_SECRET_KEY": "x"*70, "POSTGRES_SERVER": "localhost", "POSTGRES_USER": "postgres",
             "POSTGRES_PASSWORD": "postgres", "POSTGRES_DB": "val", "ENVIRONMENT": "development",
             "LOG_FILE": "", "SMTP_HOST": "x", "SMTP_USER": "a@b.c", "SMTP_PASSWORD": "x",
             "EMAILS_FROM_EMAIL": "a@b.c", "EMAILS_FROM_NAME": "x", "EMAIL_PROVIDER": "logging",
             "PAYMENT_PROVIDER": "mock", "RATE_LIMIT_ENABLED": "false", "REDIS_HOST": "localhost", "REDIS_PORT": "1"}.items():
    os.environ.setdefault(k, v)
sys.path.insert(0, "/home/user/fastapi_e-commerce_backend")
from sqlalchemy import create_engine, update, text
from sqlalchemy.orm import sessionmaker
import app.models  # noqa
from app.models.base import Base
from app.models.user import User
from app.models.address import Address
from app.models.category import Category
from app.models.product import Product, ProductVariant
from app.models.order import Order
from app.models.order_items import OrderItem
from app.services.orders import lock_stock_rows, cancel_order_and_restock

URL = "postgresql+psycopg://postgres@localhost:55441/val"
eng = create_engine(URL)
Base.metadata.drop_all(eng); Base.metadata.create_all(eng)
S = sessionmaker(eng, autoflush=False, expire_on_commit=False)

def seed():
    with S() as db:
        u = User(email="u@x.io", hashed_password="x", full_name="U", phone="+1555000")
        for col in ("is_active", "is_email_verified"):
            if hasattr(User, col): setattr(u, col, True)
        db.add(u); db.flush()
        a = Address(user_id=u.id, **{k: "x" for k in ("street", "city", "state", "postal_code", "country")} ) if True else None
        db.add(a); db.flush()
        c = Category(name="c"); db.add(c); db.flush()
        p2 = Product(name="P2", description="d"*10, price=Decimal("10"), stock_quantity=100, category_id=c.id)
        db.add(p2); db.flush()
        v2 = ProductVariant(product_id=p2.id, sku="V2", stock_quantity=100)
        db.add(v2); db.flush()
        db.commit()
        return u.id, a.id, p2.id, v2.id

def checkout(name, uid, aid, lines, hold_after_lock, started, results, key):
    """Mirror of create_order's DB statement sequence (app/api/v1/orders.py:239-369)."""
    db = S()
    try:
        o = Order(user_id=uid, status="new", payment_status="pending", shipping_address_id=aid,
                  idempotency_key=key, total_amount=Decimal("0"), currency="USD")
        db.add(o); db.flush()
        lock_stock_rows(db, lines)
        started.set()
        time.sleep(hold_after_lock)
        for pid, vid in lines:
            if vid is None:
                db.execute(update(Product).where(Product.id == pid, Product.stock_quantity >= 1)
                           .values(stock_quantity=Product.stock_quantity - 1).execution_options(synchronize_session=False))
            else:
                db.execute(update(ProductVariant).where(ProductVariant.id == vid, ProductVariant.stock_quantity >= 1)
                           .values(stock_quantity=ProductVariant.stock_quantity - 1).execution_options(synchronize_session=False))
            db.add(OrderItem(order_id=o.id, product_id=pid, variant_id=vid, quantity=1, price_at_time=Decimal("10")))
        db.commit()
        results[name] = "committed"
    except Exception as e:
        results[name] = f"{type(e).__name__}: {str(e).splitlines()[0]}"
        db.rollback()
    finally:
        db.close()

def try_import_address_fields():
    from sqlalchemy import inspect
    return [c.key for c in inspect(Address).columns]


def scenario_checkout_vs_checkout():
    uid, aid, p2, v2 = seed()
    results = {}
    a_started, b_started = threading.Event(), threading.Event()
    # A: cart = [variant V2 of P2]   -> locks V2 FOR UPDATE; FK check on P2 at INSERT.
    ta = threading.Thread(target=checkout, args=("A(variant only)", uid, aid, [(p2, v2)], 1.0, a_started, results, "key-A-0001"))
    # B: cart = [plain P2, variant V2] -> locks P2 FOR UPDATE then V2 (waits for A).
    tb = threading.Thread(target=checkout, args=("B(plain+variant)", uid, aid, [(p2, None), (p2, v2)], 0.0, b_started, results, "key-B-0001"))
    ta.start(); a_started.wait(); time.sleep(0.2); tb.start()
    ta.join(); tb.join()
    print("checkout vs checkout:", results)

def scenario_checkout_vs_cancel():
    uid, aid, p2, v2 = seed()
    # Existing NEW order with a plain P2 line and a V2 line.
    with S() as db:
        o = Order(user_id=uid, status="new", payment_status="pending", shipping_address_id=aid,
                  total_amount=Decimal("20"), currency="USD")
        db.add(o); db.flush()
        db.add_all([OrderItem(order_id=o.id, product_id=p2, quantity=1, price_at_time=Decimal("10")),
                    OrderItem(order_id=o.id, product_id=p2, variant_id=v2, quantity=1, price_at_time=Decimal("10"))])
        db.commit(); oid = o.id
    results = {}
    a_started = threading.Event()
    def cancel():
        db = S()
        try:
            from sqlalchemy import select
            order = db.execute(select(Order).where(Order.id == oid).with_for_update()).scalars().one()
            _ = order.items
            cancel_order_and_restock(db, order)   # locks P2 then V2 (waits for A)
            db.commit(); results["C(cancel)"] = "committed"
        except Exception as e:
            results["C(cancel)"] = f"{type(e).__name__}: {str(e).splitlines()[0]}"; db.rollback()
        finally:
            db.close()
    ta = threading.Thread(target=checkout, args=("A(variant only)", uid, aid, [(p2, v2)], 1.0, a_started, results, "key-A-0002"))
    tc = threading.Thread(target=cancel)
    ta.start(); a_started.wait(); time.sleep(0.2); tc.start()
    ta.join(); tc.join()
    print("checkout vs cancel:", results)

def scenario_fixed():
    """Same as checkout_vs_checkout but lock_stock_rows uses FOR NO KEY UPDATE."""
    import app.services.orders as so
    from sqlalchemy import select
    def fixed(db, lines):
        pids = sorted({p for p, v in lines if v is None}); vids = sorted({v for _, v in lines if v is not None})
        db.execute(select(Product.id).where(Product.id.in_(pids)).order_by(Product.id).with_for_update(key_share=True)).all()
        db.execute(select(ProductVariant.id).where(ProductVariant.id.in_(vids)).order_by(ProductVariant.id).with_for_update(key_share=True)).all()
    globals()["lock_stock_rows"] = fixed
    uid, aid, p2, v2 = seed()
    results = {}
    a_started, b_started = threading.Event(), threading.Event()
    ta = threading.Thread(target=checkout, args=("A(variant only)", uid, aid, [(p2, v2)], 1.0, a_started, results, "key-A-0003"))
    tb = threading.Thread(target=checkout, args=("B(plain+variant)", uid, aid, [(p2, None), (p2, v2)], 0.0, b_started, results, "key-B-0003"))
    ta.start(); a_started.wait(); time.sleep(0.2); tb.start()
    ta.join(); tb.join()
    print("FIXED (FOR NO KEY UPDATE) checkout vs checkout:", results)

if __name__ == "__main__":
    scenario_checkout_vs_checkout()
    Base.metadata.drop_all(eng); Base.metadata.create_all(eng)
    scenario_checkout_vs_cancel()
    Base.metadata.drop_all(eng); Base.metadata.create_all(eng)
    scenario_fixed()
