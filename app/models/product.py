from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    ARRAY,
    JSON,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.category import Category

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

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(index=True)
    description: Mapped[str] = mapped_column(Text)
    price: Mapped[Decimal]
    stock_quantity: Mapped[int] = mapped_column(default=0)
    # Callable defaults (list/dict, not literal [] / {}): a literal default
    # is one shared object reused by SQLAlchemy for every row that doesn't
    # set the column explicitly, so mutating one Product's images/
    # characteristics would silently mutate every other defaulted Product's
    # too (see app/models/category.py's `path` column for the same fix).
    images: Mapped[list[str]] = mapped_column(_string_array, default=list)
    characteristics: Mapped[dict[str, Any]] = mapped_column(_characteristics_json, default=dict)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    # Soft-delete flag: admin.delete_product deactivates instead of hard
    # deleting when a product has existing order history, so past orders
    # keep a valid product reference. Public listings must filter on this.
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)

    category: Mapped["Category"] = relationship(back_populates="products")
    variants: Mapped[list["ProductVariant"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )
    __table_args__ = (
        # Money and stock invariants, enforced by the database as well as
        # by the schemas: a bug or a hand-written UPDATE cannot store them.
        # Same names and conditions as migration 76d14f9f12e5, which adds
        # them to an existing database.
        CheckConstraint("price >= 0", name="ck_product_price_non_negative"),
        CheckConstraint("stock_quantity >= 0", name="ck_product_stock_non_negative"),
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

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    sku: Mapped[str] = mapped_column(unique=True, index=True)
    # e.g. {"size": "M", "color": "red"} - arbitrary key/value pairs, no
    # fixed schema, matching Product.characteristics' domain-neutral intent.
    # Callable default - see the Product.images/characteristics comment above.
    attributes: Mapped[dict[str, Any]] = mapped_column(_characteristics_json, default=dict)
    # NULL means "use the parent Product's price" - most variants of a
    # priced-per-unit product won't need a per-variant override.
    price_override: Mapped[Decimal | None]
    stock_quantity: Mapped[int] = mapped_column(default=0)
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)

    product: Mapped["Product"] = relationship(back_populates="variants")

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
