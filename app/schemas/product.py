from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    PlainSerializer,
    ValidationInfo,
    field_validator,
)

from app.schemas.common import Int32, Int32Max
from app.schemas.order import ORDER_AMOUNT_DECIMAL_PLACES, ORDER_AMOUNT_MAX_DIGITS

# An image URL is validated as an HttpUrl but kept as the plain string: it
# is stored in a string-array column and returned as that string. Without
# the serializer, Pydantic sees a str where the field's schema says HttpUrl
# and warns (PydanticSerializationUnexpectedValue) on every dump.
# A plain serializer is the documented way to say "serialize this field
# yourself" (Pydantic docs, Serialization > Field serializers).
ImageUrl = Annotated[
    HttpUrl,
    AfterValidator(str),
    PlainSerializer(str, return_type=str),
]


class ProductCharacteristic(BaseModel):
    """Schema for dynamic product characteristics"""
    name: str = Field(..., min_length=1, max_length=100)
    value: Any
    unit: str | None = None
    filterable: bool = True

class ProductBase(BaseModel):
    name: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10)
    price: Decimal = Field(
        ..., ge=0, max_digits=ORDER_AMOUNT_MAX_DIGITS, decimal_places=ORDER_AMOUNT_DECIMAL_PLACES
    )
    stock_quantity: Int32Max = Field(..., ge=0)
    category_id: Int32
    images: list[ImageUrl] = Field(default_factory=list)
    characteristics: dict[str, ProductCharacteristic] = Field(default_factory=dict)

    @field_validator('price')
    @classmethod
    def validate_price(cls, v):
        """Ensure price has exactly 2 decimal places"""
        return Decimal(str(v)).quantize(Decimal('0.01'))

    @field_validator('images')
    @classmethod
    def validate_images(cls, v):
        """Ensure we have at least one image and no duplicates, convert HttpUrl to string"""
        if not v:
            raise ValueError('At least one image is required')
        # Convert HttpUrl objects to strings and remove duplicates
        return list(dict.fromkeys(str(url) for url in v))

class ProductCreate(ProductBase):
    pass

class ProductUpdate(BaseModel):
    name: str | None = Field(None, min_length=3, max_length=200)
    description: str | None = Field(None, min_length=10)
    price: Decimal | None = Field(
        None, ge=0, max_digits=ORDER_AMOUNT_MAX_DIGITS, decimal_places=ORDER_AMOUNT_DECIMAL_PLACES
    )
    stock_quantity: Int32Max | None = Field(None, ge=0)
    category_id: Int32 | None = None
    images: list[ImageUrl] | None = None
    characteristics: dict[str, ProductCharacteristic] | None = None

    @field_validator('name', 'description', 'price', 'stock_quantity', 'category_id', 'images', 'characteristics')
    @classmethod
    def reject_null(cls, v):
        """Fields may be omitted, but every one maps to a NOT NULL column:
        an explicit null must be a 422, not a database error."""
        if v is None:
            raise ValueError('may be omitted but cannot be null')
        return v

    @field_validator('price')
    @classmethod
    def validate_price(cls, v):
        if v is None:
            return v
        return Decimal(str(v)).quantize(Decimal('0.01'))

    @field_validator('images')
    @classmethod
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
    items: list[ProductResponse]
    total: int
    page: int
    size: int
    has_more: bool

    model_config = ConfigDict(from_attributes=True)

class ProductFilter(BaseModel):
    """Schema for product filtering"""
    category_id: Int32 | None = None
    min_price: Decimal | None = Field(None, ge=0)
    max_price: Decimal | None = Field(None, ge=0)
    in_stock: bool | None = None
    characteristics: dict[str, Any] | None = None
    # Bounded like ProductSearch.query: a search term, not a document.
    search_query: str | None = Field(None, max_length=100)
    sort_by: str | None = Field(None, pattern="^(name|price|created_at)_(asc|desc)$")
    page: Int32Max = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)

    @field_validator('max_price')
    @classmethod
    def validate_price_range(cls, v, info: ValidationInfo):
        if v is not None and 'min_price' in info.data and info.data['min_price'] is not None:
            if v < info.data['min_price']:
                raise ValueError('max_price must be greater than min_price')
        return v

class ProductSearch(BaseModel):
    """Schema for product search"""
    query: str = Field(..., min_length=3, max_length=100)
    category_id: Int32 | None = None
    page: Int32Max = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)


class ProductVariantBase(BaseModel):
    """Shared fields for creating/reading a product variant.

    `attributes` is intentionally free-form (e.g. {"size": "M", "color":
    "red"}) rather than fixed columns, so this stays usable for any product
    domain, not just apparel.
    """
    sku: str = Field(..., min_length=1, max_length=100)
    attributes: dict[str, Any] = Field(default_factory=dict)
    price_override: Decimal | None = Field(
        None, ge=0, max_digits=ORDER_AMOUNT_MAX_DIGITS, decimal_places=ORDER_AMOUNT_DECIMAL_PLACES
    )
    stock_quantity: Int32Max = Field(0, ge=0)
    is_active: bool = True

    @field_validator('price_override')
    @classmethod
    def validate_price_override(cls, v):
        if v is None:
            return v
        return Decimal(str(v)).quantize(Decimal('0.01'))

class ProductVariantCreate(ProductVariantBase):
    pass

class ProductVariantUpdate(BaseModel):
    """Schema for updating an existing product variant. All fields optional."""
    sku: str | None = Field(None, min_length=1, max_length=100)
    attributes: dict[str, Any] | None = None
    price_override: Decimal | None = Field(
        None, ge=0, max_digits=ORDER_AMOUNT_MAX_DIGITS, decimal_places=ORDER_AMOUNT_DECIMAL_PLACES
    )
    stock_quantity: Int32Max | None = Field(None, ge=0)
    is_active: bool | None = None

    @field_validator('sku', 'attributes', 'stock_quantity', 'is_active')
    @classmethod
    def reject_null(cls, v):
        """NOT NULL columns. price_override is left out on purpose: null
        clears it (the variant then inherits the product's price)."""
        if v is None:
            raise ValueError('may be omitted but cannot be null')
        return v

    @field_validator('price_override')
    @classmethod
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
    items: list[ProductVariantResponse]
    total: int

    model_config = ConfigDict(from_attributes=True)

