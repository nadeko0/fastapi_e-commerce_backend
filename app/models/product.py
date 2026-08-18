from datetime import datetime
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Numeric, Text, JSON, ARRAY, Boolean, Index
from sqlalchemy.orm import relationship

from app.models.base import Base

# ARRAY is Postgres-only; SQLite (used for the in-memory test DB) has no
# array column type, so this column falls back to JSON there. Behavior is
# equivalent for a Python-side list of strings either way.
_string_array = ARRAY(String).with_variant(JSON(), "sqlite")

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    stock_quantity = Column(Integer, nullable=False, default=0)
    images = Column(_string_array, nullable=False, default=[])
    characteristics = Column(JSON, nullable=False, default={})
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    # Soft-delete flag: admin.delete_product deactivates instead of hard
    # deleting when a product has existing order history, so past orders
    # keep a valid product reference. Public listings must filter on this.
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    category = relationship("Category", back_populates="products")
    __table_args__ = (
        Index('idx_product_name_description', 'name', 'description'),
        Index('idx_product_price', 'price'),
        Index('idx_product_category', 'category_id'),
        Index('idx_product_active', 'is_active'),
    )