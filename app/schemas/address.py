import re
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import Int32


class AddressType(str, Enum):
    HOME = "home"
    WORK = "work"
    OTHER = "other"

# Mirrors app.schemas.user.UserBase's phone validation exactly - both
# represent the same "phone number" concept and previously diverged:
# UserBase.phone was validated (+digits, 5-15 digits) while
# AddressBase/AddressUpdate.delivery_phone accepted any string unvalidated.
def _validate_phone(v: str | None) -> str | None:
    if not v:
        return v
    phone = re.sub(r'\D', '', v)
    if not 5 <= len(phone) <= 15:
        raise ValueError('Invalid phone number length')
    return f"+{phone}"

class AddressBase(BaseModel):
    street: str = Field(..., min_length=5, max_length=255)
    city: str = Field(..., min_length=2, max_length=100)
    state: str = Field(..., min_length=2, max_length=100)
    postal_code: str = Field(..., min_length=3, max_length=20)
    country: str = Field(..., min_length=2, max_length=100)
    address_type: AddressType = Field(default=AddressType.HOME)
    is_default: bool = Field(default=False)
    delivery_instructions: str | None = Field(None, max_length=500)
    delivery_phone: str | None = None

    @field_validator('postal_code')
    @classmethod
    def validate_postal_code(cls, v):
        v = ''.join(v.split())
        if not 3 <= len(v) <= 20:
            raise ValueError('Invalid postal code length')
        return v

    @field_validator('country')
    @classmethod
    def validate_country(cls, v):
        if len(v) == 2:
            return v.upper()
        return v.title()

    @field_validator('delivery_phone')
    @classmethod
    def validate_delivery_phone(cls, v):
        return _validate_phone(v)

class AddressCreate(AddressBase):
    pass

class AddressUpdate(BaseModel):
    street: str | None = Field(None, min_length=5, max_length=255)
    city: str | None = Field(None, min_length=2, max_length=100)
    state: str | None = Field(None, min_length=2, max_length=100)
    postal_code: str | None = Field(None, min_length=3, max_length=20)
    country: str | None = Field(None, min_length=2, max_length=100)
    address_type: AddressType | None = None
    is_default: bool | None = None
    delivery_instructions: str | None = Field(None, max_length=500)
    delivery_phone: str | None = None

    @field_validator('street', 'city', 'state', 'postal_code', 'country', 'address_type', 'is_default')
    @classmethod
    def reject_null(cls, v):
        """NOT NULL columns. delivery_instructions and delivery_phone are
        nullable: null clears them."""
        if v is None:
            raise ValueError('may be omitted but cannot be null')
        return v

    @field_validator('postal_code')
    @classmethod
    def validate_postal_code(cls, v):
        if v is None:
            return v
        v = ''.join(v.split())
        if not 3 <= len(v) <= 20:
            raise ValueError('Invalid postal code length')
        return v

    @field_validator('country')
    @classmethod
    def validate_country(cls, v):
        if v is None:
            return v
        # Basic country code validation (can be enhanced with a proper country list)
        if len(v) == 2:
            return v.upper()
        return v.title()

    @field_validator('delivery_phone')
    @classmethod
    def validate_delivery_phone(cls, v):
        return _validate_phone(v)

class AddressResponse(AddressBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class AddressListResponse(BaseModel):
    """Schema for list of addresses with pagination"""
    items: list[AddressResponse]
    total: int
    page: int
    size: int
    has_more: bool

    model_config = ConfigDict(from_attributes=True)

class SetDefaultAddress(BaseModel):
    """Schema for setting an address as default"""
    address_id: Int32 = Field(..., description="ID of the address to set as default")
