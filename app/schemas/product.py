from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, validator


class ProductCharacteristic(BaseModel):
    """Schema for dynamic product characteristics"""
    name: str = Field(..., min_length=1, max_length=100)
    value: Any
    unit: Optional[str] = None
    filterable: bool = True

class ProductBase(BaseModel):
    name: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10)
    price: Decimal = Field(..., ge=0)
    stock_quantity: int = Field(..., ge=0)
    category_id: int
    images: List[HttpUrl] = Field(default_factory=list)
    characteristics: Dict[str, ProductCharacteristic] = Field(default_factory=dict)

    @validator('price')
    def validate_price(cls, v):
        """Ensure price has exactly 2 decimal places"""
        return Decimal(str(v)).quantize(Decimal('0.01'))

    @validator('images')
    def validate_images(cls, v):
        """Ensure we have at least one image and no duplicates, convert HttpUrl to string"""
        if not v:
            raise ValueError('At least one image is required')
        # Convert HttpUrl objects to strings and remove duplicates
        return list(dict.fromkeys(str(url) for url in v))

class ProductCreate(ProductBase):
    pass

class ProductUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=3, max_length=200)
    description: Optional[str] = Field(None, min_length=10)
    price: Optional[Decimal] = Field(None, ge=0)
    stock_quantity: Optional[int] = Field(None, ge=0)
    category_id: Optional[int] = None
    images: Optional[List[HttpUrl]] = None
    characteristics: Optional[Dict[str, ProductCharacteristic]] = None

    @validator('price')
    def validate_price(cls, v):
        if v is None:
            return v
        return Decimal(str(v)).quantize(Decimal('0.01'))

    @validator('images')
    def validate_images(cls, v):
        if v is None:
            return v
        if not v:
            raise ValueError('At least one image is required')
        # Convert HttpUrl objects to strings and remove duplicates
        return list(dict.fromkeys(str(url) for url in v))

class ProductInDB(ProductBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class ProductResponse(ProductInDB):
    category_name: str

    model_config = ConfigDict(from_attributes=True)

class ProductListResponse(BaseModel):
    """Schema for list of products with pagination"""
    items: List[ProductResponse]
    total: int
    page: int
    size: int
    has_more: bool

    model_config = ConfigDict(from_attributes=True)

class ProductFilter(BaseModel):
    """Schema for product filtering"""
    category_id: Optional[int] = None
    min_price: Optional[Decimal] = Field(None, ge=0)
    max_price: Optional[Decimal] = Field(None, ge=0)
    in_stock: Optional[bool] = None
    characteristics: Optional[Dict[str, Any]] = None
    search_query: Optional[str] = None
    sort_by: Optional[str] = Field(None, pattern="^(name|price|created_at)_(asc|desc)$")
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)

    @validator('max_price')
    def validate_price_range(cls, v, values):
        if v is not None and 'min_price' in values and values['min_price'] is not None:
            if v < values['min_price']:
                raise ValueError('max_price must be greater than min_price')
        return v

class ProductSearch(BaseModel):
    """Schema for product search"""
    query: str = Field(..., min_length=3)
    category_id: Optional[int] = None
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)


class ProductVariantBase(BaseModel):
    """Shared fields for creating/reading a product variant.

    `attributes` is intentionally free-form (e.g. {"size": "M", "color":
    "red"}) rather than fixed columns, so this stays usable for any product
    domain, not just apparel.
    """
    sku: str = Field(..., min_length=1, max_length=100)
    attributes: Dict[str, Any] = Field(default_factory=dict)
    price_override: Optional[Decimal] = Field(None, ge=0)
    stock_quantity: int = Field(0, ge=0)
    is_active: bool = True

    @validator('price_override')
    def validate_price_override(cls, v):
        if v is None:
            return v
        return Decimal(str(v)).quantize(Decimal('0.01'))

class ProductVariantCreate(ProductVariantBase):
    pass

class ProductVariantUpdate(BaseModel):
    """Schema for updating an existing product variant. All fields optional."""
    sku: Optional[str] = Field(None, min_length=1, max_length=100)
    attributes: Optional[Dict[str, Any]] = None
    price_override: Optional[Decimal] = Field(None, ge=0)
    stock_quantity: Optional[int] = Field(None, ge=0)
    is_active: Optional[bool] = None

    @validator('price_override')
    def validate_price_override(cls, v):
        if v is None:
            return v
        return Decimal(str(v)).quantize(Decimal('0.01'))

class ProductVariantResponse(ProductVariantBase):
    id: int
    product_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class ProductVariantListResponse(BaseModel):
    """Schema for a list of variants under a product (no pagination - a
    single product's variant count is expected to be small)."""
    items: List[ProductVariantResponse]
    total: int

    model_config = ConfigDict(from_attributes=True)

