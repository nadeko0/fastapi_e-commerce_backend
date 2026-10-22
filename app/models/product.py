from sqlalchemy import (
    ARRAY,
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship

from app.core.clock import UTCDateTime, utc_now
from app.models.base import Base

# ARRAY is Postgres-only; SQLite (used for the in-memory test DB) has no
# array column type, so this column falls back to JSON there. Behavior is
# equivalent for a Python-side list of strings either way.
_string_array = ARRAY(String).with_variant(JSON(), "sqlite")

# The generic sqlalchemy.JSON type's [key] indexing comparator has no
# .astext accessor - only the Postgres-specific JSON/JSONB types do. Since
# list_products (app/api/v1/products.py) filters on
# Product.characteristics[key].astext, plain JSON here would raise
# AttributeError on every request using the characteristics filter against
# real Postgres (never caught by the SQLite-backed test suite, which
# doesn't exercise this path). JSONB is also the idiomatic/indexable choice
# for Postgres; SQLite keeps generic JSON, matching the pattern above.
_characteristics_json = JSON().with_variant(JSONB(), "postgresql")

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    stock_quantity = Column(Integer, nullable=False, default=0)
    # Callable defaults (list/dict, not literal [] / {}): a literal default
    # is one shared object reused by SQLAlchemy for every row that doesn't
    # set the column explicitly, so mutating one Product's images/
    # characteristics would silently mutate every other defaulted Product's
    # too (see app/models/category.py's `path` column for the same fix).
    images = Column(_string_array, nullable=False, default=list)
    characteristics = Column(_characteristics_json, nullable=False, default=dict)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    # Soft-delete flag: admin.delete_product deactivates instead of hard
    # deleting when a product has existing order history, so past orders
    # keep a valid product reference. Public listings must filter on this.
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

    category = relationship("Category", back_populates="products")
    variants = relationship(
        "ProductVariant", back_populates="product", cascade="all, delete-orphan"
    )
    __table_args__ = (
        # Money and stock invariants, enforced by the database as well as
        # by the schemas: a bug or a hand-written UPDATE cannot store them.
        # Same names and conditions as migration 76d14f9f12e5, which adds
        # them to an existing database.
        CheckConstraint("price >= 0", name="ck_product_price_non_negative"),
        CheckConstraint("stock_quantity >= 0", name="ck_product_stock_non_negative"),
        Index('idx_product_name_description', 'name', 'description'),
        Index('idx_product_price', 'price'),
        Index('idx_product_category', 'category_id'),
        Index('idx_product_active', 'is_active'),
    )


class ProductVariant(Base):
    """
    An optional purchasable variation of a Product (e.g. a specific
    size/color combination), keyed by its own SKU. A Product with no
    variants is a complete, ordinary "simple" product on its own - nothing
    here is required, and Product.price/Product.stock_quantity keep working
    exactly as before for that common case. `attributes` is deliberately
    free-form JSON (not fixed columns like `size`/`color`) so this stays
    usable for any product domain, not just apparel.

    Cart/checkout (app/api/v1/cart.py, app/api/v1/orders.py) are
    variant-aware: an optional variant_id on a cart line/OrderItem prices
    and decrements/restocks stock against this ProductVariant instead of
    the parent Product.
    """

    __tablename__ = "product_variants"

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    sku = Column(String, nullable=False, unique=True, index=True)
    # e.g. {"size": "M", "color": "red"} - arbitrary key/value pairs, no
    # fixed schema, matching Product.characteristics' domain-neutral intent.
    # Callable default - see the Product.images/characteristics comment above.
    attributes = Column(_characteristics_json, nullable=False, default=dict)
    # NULL means "use the parent Product's price" - most variants of a
    # priced-per-unit product won't need a per-variant override.
    price_override = Column(Numeric(10, 2), nullable=True)
    stock_quantity = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

    product = relationship("Product", back_populates="variants")

    __table_args__ = (
        # See Product.__table_args__.
        CheckConstraint("stock_quantity >= 0", name="ck_product_variant_stock_non_negative"),
        CheckConstraint(
            "price_override IS NULL OR price_override >= 0",
            name="ck_product_variant_price_override_non_negative",
        ),
        Index("idx_product_variant_product", "product_id"),
        Index("idx_product_variant_active", "is_active"),
    )
