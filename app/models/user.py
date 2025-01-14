from datetime import datetime
from sqlalchemy import Boolean, Column, Integer, String, DateTime, JSON, Index, CheckConstraint
from sqlalchemy.orm import relationship

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
    role = Column(*create_string_enum(UserRole, "role")[0:1], nullable=False, server_default="client")
    is_active = Column(Boolean, nullable=False, default=True)
    is_email_verified = Column(Boolean, nullable=False, default=False)
    email_verification_date = Column(DateTime)
    
    # Essential GDPR Fields
    gdpr_consent = Column(Boolean, nullable=False, default=False)
    gdpr_consent_date = Column(DateTime)
    privacy_policy_accepted = Column(Boolean, nullable=False, default=False)
    privacy_policy_accepted_date = Column(DateTime)
    marketing_consent = Column(Boolean, nullable=False, default=False)
    marketing_consent_date = Column(DateTime)
    data_deletion_requested = Column(Boolean, nullable=False, default=False)
    data_deletion_date = Column(DateTime)
    data_retention_period = Column(Integer, nullable=False, default=settings.USER_DATA_RETENTION_DAYS)
    consent_history = Column(JSON, nullable=False, default=[])
    data_export_requests = Column(JSON, nullable=False, default=[])
    last_login = Column(DateTime)
    last_activity_date = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

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
    )

    def __repr__(self):
        return f"<User {self.email} ({self.role})>"