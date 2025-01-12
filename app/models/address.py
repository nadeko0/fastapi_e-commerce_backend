from datetime import datetime
import enum
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Enum, Index
from sqlalchemy.orm import relationship

from app.models.base import Base

class AddressType(str, enum.Enum):
    HOME = "home"
    WORK = "work"
    OTHER = "other"

class Address(Base):
    __tablename__ = "addresses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    street = Column(String, nullable=False)
    city = Column(String, nullable=False)
    state = Column(String, nullable=False)
    postal_code = Column(String, nullable=False)
    country = Column(String, nullable=False)
    
    address_type = Column(Enum(AddressType), nullable=False, default=AddressType.HOME)
    is_default = Column(Boolean, nullable=False, default=False)
    delivery_phone = Column(String)
    delivery_instructions = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    
    user = relationship("User", back_populates="addresses")
    __table_args__ = (
        Index('idx_address_user', 'user_id'),
        Index('idx_address_default', 'user_id', 'is_default'),
        Index('idx_address_active', 'is_active'),
        Index(
            'uix_active_user_address',
            'user_id', 'street', 'city', 'state', 'postal_code', 'country', 'is_active',
            unique=True,
            postgresql_where=Column('is_active') == True
        ),
    )

    def __repr__(self):
        return f"<Address {self.street}, {self.city} ({self.address_type})>"