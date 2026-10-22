from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.orm import relationship

from app.core.clock import UTCDateTime, utc_now
from app.core.config import settings
from app.models.base import Base
from app.models.enums import UserRole, create_string_enum


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True, index=True, nullable=False)
    hashed_password = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    phone = Column(String, nullable=True)
    role = Column(create_string_enum(UserRole, "role"), nullable=False, server_default="client")
    is_active = Column(Boolean, nullable=False, default=True)
    is_email_verified = Column(Boolean, nullable=False, default=False)
    email_verification_date = Column(UTCDateTime)

    # Essential GDPR Fields
    gdpr_consent = Column(Boolean, nullable=False, default=False)
    gdpr_consent_date = Column(UTCDateTime)
    privacy_policy_accepted = Column(Boolean, nullable=False, default=False)
    privacy_policy_accepted_date = Column(UTCDateTime)
    marketing_consent = Column(Boolean, nullable=False, default=False)
    marketing_consent_date = Column(UTCDateTime)
    data_deletion_requested = Column(Boolean, nullable=False, default=False)
    data_deletion_date = Column(UTCDateTime)
    data_retention_period = Column(Integer, nullable=False, default=settings.USER_DATA_RETENTION_DAYS)
    # Callable defaults - see app/models/category.py's `path` column comment:
    # a literal [] would be one list object shared (and mutated together)
    # across every User row that doesn't set this column explicitly.
    consent_history = Column(JSON, nullable=False, default=list)
    data_export_requests = Column(JSON, nullable=False, default=list)
    last_login = Column(UTCDateTime)
    last_activity_date = Column(UTCDateTime, default=utc_now, onupdate=utc_now)
    created_at = Column(UTCDateTime, default=utc_now, nullable=False)
    updated_at = Column(UTCDateTime, default=utc_now, onupdate=utc_now, nullable=False)

    addresses = relationship("Address", back_populates="user", cascade="all, delete-orphan")
    orders = relationship("Order", back_populates="user", cascade="all, delete-orphan")
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
