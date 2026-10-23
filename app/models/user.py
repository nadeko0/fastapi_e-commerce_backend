from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Index,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now
from app.core.config import settings
from app.models.base import Base
from app.models.enums import UserRole

if TYPE_CHECKING:
    from app.models.address import Address
    from app.models.order import Order


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    hashed_password: Mapped[str]
    full_name: Mapped[str | None]
    phone: Mapped[str | None]
    role: Mapped[str] = mapped_column(server_default="client")
    is_active: Mapped[bool] = mapped_column(default=True)
    is_email_verified: Mapped[bool] = mapped_column(default=False)
    email_verification_date: Mapped[datetime | None]

    # Essential GDPR Fields
    gdpr_consent: Mapped[bool] = mapped_column(default=False)
    gdpr_consent_date: Mapped[datetime | None]
    privacy_policy_accepted: Mapped[bool] = mapped_column(default=False)
    privacy_policy_accepted_date: Mapped[datetime | None]
    marketing_consent: Mapped[bool] = mapped_column(default=False)
    marketing_consent_date: Mapped[datetime | None]
    data_deletion_requested: Mapped[bool] = mapped_column(default=False)
    data_deletion_date: Mapped[datetime | None]
    data_retention_period: Mapped[int] = mapped_column(default=settings.USER_DATA_RETENTION_DAYS)
    # Callable defaults - see app/models/category.py's `path` column comment:
    # a literal [] would be one list object shared (and mutated together)
    # across every User row that doesn't set this column explicitly.
    consent_history: Mapped[list[Any]] = mapped_column(JSON, default=list)
    data_export_requests: Mapped[list[Any]] = mapped_column(JSON, default=list)
    last_login: Mapped[datetime | None]
    last_activity_date: Mapped[datetime | None] = mapped_column(default=utc_now, onupdate=utc_now)
    created_at: Mapped[datetime] = mapped_column(default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(default=utc_now, onupdate=utc_now)

    addresses: Mapped[list["Address"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    orders: Mapped[list["Order"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    __table_args__ = (
        CheckConstraint(
            f"role IN {tuple(r.value for r in UserRole)}",
            name="ck_user_role"
        ),
        Index('idx_user_email_verified', 'is_email_verified'),
        Index('idx_user_role', 'role'),
        Index('idx_user_gdpr', 'gdpr_consent', 'privacy_policy_accepted', 'data_deletion_requested'),
        Index('idx_user_activity', 'last_login', 'last_activity_date', 'is_active'),
        # One account per mailbox, whatever the case: ix_users_email above
        # is case-sensitive, and "Case@x.com" next to "case@x.com" were two
        # accounts. Created by migration 3f6c2a9d1b7e, which first refuses
        # to run while such duplicates exist. Also what the lookups by email
        # (lower(email) = ...) in app/api/v1/users.py use.
        Index('uq_users_email_lower', func.lower(email), unique=True),
    )

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"
