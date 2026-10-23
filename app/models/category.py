from sqlalchemy import ARRAY, JSON, Column, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship

from app.core.clock import UTCDateTime, utc_now
from app.models.base import Base

# See app/models/product.py for why ARRAY needs a SQLite fallback.
_int_array = ARRAY(Integer).with_variant(JSON(), "sqlite")

class Category(Base):
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, index=True)
    description = Column(Text)
    parent_id = Column(Integer, ForeignKey('categories.id'), nullable=True)
    # Callable defaults (list/dict, not literal [] / {}): SQLAlchemy reuses
    # the exact same object as the default for every row that doesn't set
    # this column explicitly, so a literal [] here would mean every such
    # Category shares (and mutates) one list instance across the session.
    path = Column(_int_array, nullable=False, default=list)
    level = Column(Integer, nullable=False, default=0)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

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
