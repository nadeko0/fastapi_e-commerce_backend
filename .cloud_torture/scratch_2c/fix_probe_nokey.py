"""Scratch pytest plugin (not part of the suite): prototype of the proposed
fix for the checkout deadlock - lock_stock_rows taking FOR NO KEY UPDATE
(SQLAlchemy: with_for_update(key_share=True)) instead of FOR UPDATE - applied
by monkeypatching at collection time. app/ is not edited."""
from sqlalchemy import select


def pytest_collection_finish(session):
    import app.api.v1.orders as orders_module
    import app.services.orders as services
    from app.models.product import Product, ProductVariant

    def lock_stock_rows(db, lines):
        product_ids = sorted({p for p, v in lines if v is None})
        variant_ids = sorted({v for _, v in lines if v is not None})
        db.execute(select(Product.id).where(Product.id.in_(product_ids))
                   .order_by(Product.id).with_for_update(key_share=True)).all()
        db.execute(select(ProductVariant.id).where(ProductVariant.id.in_(variant_ids))
                   .order_by(ProductVariant.id).with_for_update(key_share=True)).all()

    services.lock_stock_rows = lock_stock_rows
    orders_module.lock_stock_rows = lock_stock_rows
