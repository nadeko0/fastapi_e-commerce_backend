from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, ConfigDict, validator
from enum import Enum

class AddressType(str, Enum):
    HOME = "home"
    WORK = "work"
    OTHER = "other"

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

class AddressInDB(AddressBase):
    id: int
    user_id: int
    created_at: datetime
    updated_at: datetime
    is_active: bool = True

    model_config = ConfigDict(from_attributes=True)

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