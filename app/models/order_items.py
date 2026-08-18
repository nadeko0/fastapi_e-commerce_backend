from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric
from sqlalchemy.orm import relationship

from app.models.base import Base


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    price_at_time = Column(Numeric(10, 2), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    order = relationship("Order", back_populates="items")
    product = relationship("Product")

    # OrderItemResponse (app/schemas/order.py) requires product_name/
    # product_image as flat fields - these read-only properties (not
    # columns) let pydantic's from_attributes/from_orm pick them up
    # straight off the ORM object via the `product` relationship, the same
    # way app/api/v1/cart.py already denormalizes product name/image onto
    # cart line items. Pre-existing gap: OrderResponse.from_orm(order)
    # previously raised a ValidationError on every order (no prior test
    # exercised order creation end-to-end to catch it).
    @property
    def product_name(self) -> str:
        return self.product.name if self.product else ""

    @property
    def product_image(self) -> str:
        return self.product.images[0] if self.product and self.product.images else ""
