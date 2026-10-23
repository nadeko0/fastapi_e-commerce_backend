from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import ARRAY, JSON, ForeignKey, Index, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now
from app.models.base import Base

if TYPE_CHECKING:
    from app.models.product import Product

# See app/models/product.py for why ARRAY needs a SQLite fallback.
_int_array = ARRAY(Integer).with_variant(JSON(), "sqlite")

class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(index=True)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[int | None] = mapped_column(ForeignKey('categories.id'))
    # Callable defaults (list/dict, not literal [] / {}): SQLAlchemy reuses
    # the exact same object as the default for every row that doesn't set
    # this column explicitly, so a literal [] here would mean every such
    # Category shares (and mutates) one list instance across the session.
    path: Mapped[list[int]] = mapped_column(_int_array, default=list)
    level: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)

    parent: Mapped["Category | None"] = relationship(back_populates="children", remote_side=[id])
    children: Mapped[list["Category"]] = relationship(back_populates="parent", cascade="all, delete-orphan")

    products: Mapped[list["Product"]] = relationship(back_populates="category", cascade="all, delete-orphan")
    __table_args__ = (
        Index('idx_category_path', 'path'),
        Index('idx_category_parent', 'parent_id'),
        Index('idx_category_level', 'level'),
    )

    def __repr__(self):
        return f"<Category {self.name} (level={self.level})>"
