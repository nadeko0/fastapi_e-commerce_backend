from datetime import datetime
from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey, Index, CheckConstraint
from sqlalchemy.orm import relationship

from app.models.base import Base
from app.models.enums import AddressType, create_string_enum

class Address(Base):
    __tablename__ = "addresses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    street = Column(String, nullable=False)
    city = Column(String, nullable=False)
    state = Column(String, nullable=False)
    postal_code = Column(String, nullable=False)
    country = Column(String, nullable=False)
    
    address_type = Column(*create_string_enum(AddressType, "address_type")[0:1], nullable=False, server_default="home")
    is_default = Column(Boolean, nullable=False, default=False)
    delivery_phone = Column(String)
    delivery_instructions = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    
    user = relationship("User", back_populates="addresses")
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
            postgresql_where=Column('is_active') == True
        ),
    )

    def __repr__(self):
        return f"<Address {self.street}, {self.city} ({self.address_type})>"