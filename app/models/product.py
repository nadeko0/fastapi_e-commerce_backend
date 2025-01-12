from datetime import datetime
from sqlalchemy import Column, Integer, String, ForeignKey, DateTime, Numeric, Text, JSON, ARRAY, Index
from sqlalchemy.orm import relationship

from app.models.base import Base

class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=False)
    price = Column(Numeric(10, 2), nullable=False)
    stock_quantity = Column(Integer, nullable=False, default=0)
    images = Column(ARRAY(String), nullable=False, default=[])
    characteristics = Column(JSON, nullable=False, default={})
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    category = relationship("Category", back_populates="products")
    __table_args__ = (
        Index('idx_product_name_description', 'name', 'description'),
        Index('idx_product_price', 'price'),
        Index('idx_product_category', 'category_id'),
    )