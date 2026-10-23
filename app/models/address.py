from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Column,
    ForeignKey,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now
from app.models.base import Base
from app.models.enums import AddressType

if TYPE_CHECKING:
    from app.models.user import User


class Address(Base):
    __tablename__ = "addresses"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))

    street: Mapped[str]
    city: Mapped[str]
    state: Mapped[str]
    postal_code: Mapped[str]
    country: Mapped[str]

    address_type: Mapped[str] = mapped_column(server_default="home")
    is_default: Mapped[bool] = mapped_column(default=False)
    delivery_phone: Mapped[str | None]
    delivery_instructions: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)
    is_active: Mapped[bool] = mapped_column(default=True)

    user: Mapped["User"] = relationship(back_populates="addresses")
    __table_args__ = (
        CheckConstraint(
            f"address_type IN {tuple(t.value for t in AddressType)}",
            name="ck_address_type"
        ),
        Index('idx_address_user', 'user_id'),
        Index('idx_address_default', 'user_id', 'is_default'),
        Index('idx_address_active', 'is_active'),
        Index(
            'uix_active_user_address',
            'user_id', 'street', 'city', 'state', 'postal_code', 'country', 'is_active',
            unique=True,
            postgresql_where=Column('is_active')
        ),
    )

    def __repr__(self):
        return f"<Address {self.street}, {self.city} ({self.address_type})>"
