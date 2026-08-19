from datetime import datetime

from sqlalchemy import ARRAY, JSON, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from app.models.base import Base

# See app/models/product.py for why ARRAY needs a SQLite fallback.
_int_array = ARRAY(Integer).with_variant(JSON(), "sqlite")

class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, index=True)
    description = Column(Text)
    parent_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    # Callable defaults (list/dict, not literal [] / {}): SQLAlchemy reuses
    # the exact same object as the default for every row that doesn't set
    # this column explicitly, so a literal [] here would mean every such
    # Category shares (and mutates) one list instance across the session.
    path = Column(_int_array, nullable=False, default=list)
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
