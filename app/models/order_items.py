from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, ForeignKey, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.order import Order
    from app.models.product import Product, ProductVariant


class OrderItem(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    # NULL means this line was for the plain product (no variant selected) -
    # the common case, and the only case that existed before variant support
    # was wired into checkout. When set, stock for this line was decremented
    # from ProductVariant.stock_quantity (not Product.stock_quantity) at
    # checkout, and must be restocked there on cancellation too.
    variant_id: Mapped[int | None] = mapped_column(ForeignKey("product_variants.id"))
    quantity: Mapped[int]
    price_at_time: Mapped[Decimal]
    created_at: Mapped[datetime] = mapped_column(default=utc_now)

    order: Mapped["Order"] = relationship(back_populates="items")
    product: Mapped["Product"] = relationship()
    variant: Mapped["ProductVariant | None"] = relationship()

    __table_args__ = (
        # See Product.__table_args__.
        CheckConstraint("quantity > 0", name="ck_order_item_quantity_positive"),
        CheckConstraint("price_at_time >= 0", name="ck_order_item_price_non_negative"),
        # PostgreSQL does not index foreign key columns by itself; these serve
        # loading an order's items and the product/variant order-history checks.
        Index("idx_order_item_order_id", "order_id"),
        Index("idx_order_item_product_id", "product_id"),
        Index("idx_order_item_variant_id", "variant_id"),
    )

    # OrderItemResponse (app/schemas/order.py) requires product_name/
    # product_image as flat fields - these read-only properties (not
    # columns) let pydantic's from_attributes pick them up
    # straight off the ORM object via the `product` relationship, the same
    # way app/api/v1/cart.py already denormalizes product name/image onto
    # cart line items. Pre-existing gap: OrderResponse.model_validate(order)
    # previously raised a ValidationError on every order (no prior test
    # exercised order creation end-to-end to catch it).
    @property
    def product_name(self) -> str:
        return self.product.name if self.product else ""

    @property
    def product_image(self) -> str:
        return self.product.images[0] if self.product and self.product.images else ""

    # Mirrors product_name/product_image above: OrderItemResponse exposes a
    # flat variant_sku field (None for the common no-variant line) rather
    # than requiring callers to join through the `variant` relationship
    # themselves.
    @property
    def variant_sku(self) -> "str | None":
        return self.variant.sku if self.variant else None
