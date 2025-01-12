from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, ForeignKey, Index, ARRAY
from sqlalchemy.orm import relationship

from app.models.base import Base

class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(Text)
    parent_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    path = Column(ARRAY(Integer), nullable=False, default=[])
    level = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    parent = relationship("Category", back_populates="children", remote_side=[id])
    children = relationship("Category", back_populates="parent", cascade="all, delete-orphan")
    
    products = relationship("Product", back_populates="category", cascade="all, delete-orphan")
    __table_args__ = (
        Index('idx_category_path', 'path'),
        Index('idx_category_parent', 'parent_id'),
        Index('idx_category_level', 'level'),
    )

    def __repr__(self):
        return f"<Category {self.name} (level={self.level})>"