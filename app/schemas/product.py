from datetime import datetime
from typing import List, Optional, Dict, Any
from decimal import Decimal
from pydantic import BaseModel, Field, ConfigDict, validator, HttpUrl

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

class StockUpdate(BaseModel):
    """Schema for updating product stock"""
    quantity: int = Field(..., ge=0)
    reason: Optional[str] = None