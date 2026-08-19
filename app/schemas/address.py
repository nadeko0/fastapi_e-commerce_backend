import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, validator


class AddressType(str, Enum):
    HOME = "home"
    WORK = "work"
    OTHER = "other"

# Mirrors app.schemas.user.UserBase's phone validation exactly - both
# represent the same "phone number" concept and previously diverged:
# UserBase.phone was validated (+digits, 5-15 digits) while
# AddressBase/AddressUpdate.delivery_phone accepted any string unvalidated.
def _validate_phone(v: Optional[str]) -> Optional[str]:
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
    delivery_instructions: Optional[str] = Field(None, max_length=500)
    delivery_phone: Optional[str] = None

    @validator('postal_code')
    def validate_postal_code(cls, v):
        v = ''.join(v.split())
        if not 3 <= len(v) <= 20:
            raise ValueError('Invalid postal code length')
        return v

    @validator('country')
    def validate_country(cls, v):
        if len(v) == 2:
            return v.upper()
        return v.title()

    @validator('delivery_phone')
    def validate_delivery_phone(cls, v):
        return _validate_phone(v)

class AddressCreate(AddressBase):
    pass

class AddressUpdate(BaseModel):
    street: Optional[str] = Field(None, min_length=5, max_length=255)
    city: Optional[str] = Field(None, min_length=2, max_length=100)
    state: Optional[str] = Field(None, min_length=2, max_length=100)
    postal_code: Optional[str] = Field(None, min_length=3, max_length=20)
    country: Optional[str] = Field(None, min_length=2, max_length=100)
    address_type: Optional[AddressType] = None
    is_default: Optional[bool] = None
    delivery_instructions: Optional[str] = Field(None, max_length=500)
    delivery_phone: Optional[str] = None

    @validator('postal_code')
    def validate_postal_code(cls, v):
        if v is None:
            return v
        v = ''.join(v.split())
        if not 3 <= len(v) <= 20:
            raise ValueError('Invalid postal code length')
        return v

    @validator('country')
    def validate_country(cls, v):
        if v is None:
            return v
        # Basic country code validation (can be enhanced with a proper country list)
        if len(v) == 2:
            return v.upper()
        return v.title()

    @validator('delivery_phone')
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
    address_id: int = Field(..., description="ID of the address to set as default")
