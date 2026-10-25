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

from app.schemas.common import Int32, Int32Max, RequestBody, StoreCurrency, input_schema
from app.schemas.order import NonNegativeMoney

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
    # No limit on the name: it is an input rule, ProductCharacteristicInput's.
    name: str
    value: Any
    unit: str | None = None
    filterable: bool = True

class ProductCharacteristicInput(RequestBody, ProductCharacteristic):
    """ProductCharacteristic as a client sends it: no unknown members."""
    name: str = Field(..., min_length=1, max_length=100)


class ProductBase(BaseModel):
    # No limits on the text: they are input rules (ProductCreate,
    # ProductUpdate), and ProductBase is also what the catalog answers with.
    name: str
    description: str
    price: NonNegativeMoney
    stock_quantity: Int32Max = Field(..., ge=0)
    category_id: Int32
    # No minimum here: ProductBase is also what the catalog answers with, and
    # a row without images (stored before the rule, or edited in the database)
    # must stay readable. The rule for what a client sends is ProductCreate's.
    images: list[ImageUrl] = Field(default_factory=list)
    characteristics: dict[str, ProductCharacteristic] = Field(default_factory=dict)

class ProductCreate(RequestBody, ProductBase):
    name: str = Field(..., min_length=3, max_length=200)
    description: str = Field(..., min_length=10)
    # Required, not defaulted: Pydantic does not run validators on a default,
    # so an omitted `images` would have skipped the rule below and stored a
    # product without images.
    images: list[ImageUrl]
    characteristics: dict[str, ProductCharacteristicInput] = Field(default_factory=dict)

    model_config = ConfigDict(json_schema_extra=input_schema(rules={"images": {"minItems": 1}}))

    @field_validator('images')
    @classmethod
    def validate_images(cls, v):
        """Ensure we have at least one image and no duplicates, convert HttpUrl to string"""
        if not v:
            raise ValueError('At least one image is required')
        # Convert HttpUrl objects to strings and remove duplicates
        return list(dict.fromkeys(str(url) for url in v))

class ProductUpdate(RequestBody):
    name: str | None = Field(None, min_length=3, max_length=200)
    description: str | None = Field(None, min_length=10)
    price: NonNegativeMoney | None = None
    stock_quantity: Int32Max | None = Field(None, ge=0)
    category_id: Int32 | None = None
    images: list[ImageUrl] | None = None
    characteristics: dict[str, ProductCharacteristicInput] | None = None

    # Every member may be left out, none may be null (reject_null), and an
    # empty image list is refused (validate_images).
    model_config = ConfigDict(json_schema_extra=input_schema(
        not_null=("name", "description", "price", "stock_quantity", "category_id", "images", "characteristics"),
        rules={"images": {"minItems": 1}},
    ))

    @field_validator('name', 'description', 'price', 'stock_quantity', 'category_id', 'images', 'characteristics')
    @classmethod
    def reject_null(cls, v):
        """Fields may be omitted, but every one maps to a NOT NULL column:
        an explicit null must be a 422, not a database error."""
        if v is None:
            raise ValueError('may be omitted but cannot be null')
        return v

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
    currency: StoreCurrency

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
        if (
            v is not None
            and 'min_price' in info.data
            and info.data['min_price'] is not None
            and v < info.data['min_price']
        ):
            raise ValueError('max_price must be greater than min_price')
        return v

class ProductSearch(BaseModel):
    """Schema for product search"""
    query: str = Field(..., min_length=3, max_length=100)
    category_id: Int32 | None = None
    page: Int32Max = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)


class ProductVariantBase(BaseModel):
    """Shared fields for creating/reading a product variant. No limit on the
    sku here: it is an input rule (ProductVariantCreate, ProductVariantUpdate),
    and a stored variant that breaks it must still be readable.

    `attributes` is intentionally free-form (e.g. {"size": "M", "color":
    "red"}) rather than fixed columns, so this stays usable for any product
    domain, not just apparel.
    """
    sku: str
    attributes: dict[str, Any] = Field(default_factory=dict)
    price_override: NonNegativeMoney | None = None
    stock_quantity: Int32Max = Field(0, ge=0)
    is_active: bool = True

class ProductVariantCreate(RequestBody, ProductVariantBase):
    sku: str = Field(..., min_length=1, max_length=100)

class ProductVariantUpdate(RequestBody):
    """Schema for updating an existing product variant. All fields optional."""
    sku: str | None = Field(None, min_length=1, max_length=100)
    attributes: dict[str, Any] | None = None
    price_override: NonNegativeMoney | None = None
    stock_quantity: Int32Max | None = Field(None, ge=0)
    is_active: bool | None = None

    # price_override is the one member that takes null (it clears the override).
    model_config = ConfigDict(json_schema_extra=input_schema(
        not_null=("sku", "attributes", "stock_quantity", "is_active"),
    ))

    @field_validator('sku', 'attributes', 'stock_quantity', 'is_active')
    @classmethod
    def reject_null(cls, v):
        """NOT NULL columns. price_override is left out on purpose: null
        clears it (the variant then inherits the product's price)."""
        if v is None:
            raise ValueError('may be omitted but cannot be null')
        return v

class ProductVariantResponse(ProductVariantBase):
    id: int
    currency: StoreCurrency
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

