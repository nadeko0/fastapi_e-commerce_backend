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
    WithJsonSchema,
    field_validator,
)

from app.schemas.common import (
    Int32,
    Int32Max,
    RequestBody,
    ResponseBody,
    StoreCurrency,
    input_schema,
)
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


class ProductCharacteristic(ResponseBody):
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
    # The input type of a characteristic, narrower than ProductBase's: Pydantic
    # validates by this annotation, mypy sees an incompatible override of a dict.
    characteristics: dict[str, ProductCharacteristicInput] = Field(default_factory=dict)  # type: ignore[assignment]

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

class ProductResponse(ResponseBody, ProductInDB):
    category_name: str
    currency: StoreCurrency

    model_config = ConfigDict(from_attributes=True)

class ProductListItem(ProductResponse):
    """A product in the list and search results: the product, plus whether it
    is sold by variants (the variants themselves are on GET /products/{id})."""
    has_variants: bool = Field(
        ...,
        description=(
            "True when the product has at least one active variant. Such a product is bought "
            "through a variant: open GET /products/{id} for them."
        ),
    )


class ProductListResponse(BaseModel):
    """Schema for list of products with pagination"""
    items: list[ProductListItem]
    total: int
    page: int
    size: int
    has_more: bool

    model_config = ConfigDict(from_attributes=True)

# A price in the query string. Pydantic's own schema for a Decimal that takes
# input is "number or string", which generators turn into `number | string`;
# a query value is text that is read as a number, so the document says number.
# Schema only: the field validates exactly as before.
QueryPrice = Annotated[
    Decimal,
    Field(ge=0, le=Decimal("99999999.99"), decimal_places=6),
    WithJsonSchema(
        {
            "type": "number",
            "minimum": 0,
            "maximum": 99999999.99,
            "description": "A price, at most 6 fraction digits.",
        },
        mode="validation",
    ),
]


class ProductFilter(BaseModel):
    """Schema for product filtering"""
    category_id: Int32 | None = None
    min_price: QueryPrice | None = None
    max_price: QueryPrice | None = None
    in_stock: bool | None = None
    characteristics: dict[str, Any] | None = None

    @field_validator("min_price", "max_price")
    @classmethod
    def _zero_has_no_exponent(cls, v):
        return Decimal(0) if v == 0 else v

    # Bounded like ProductSearch.query: a search term, not a document.
    search_query: str | None = Field(None, max_length=100)
    # field_direction in one value (not sort_by plus sort_order, as the other
    # lists); anything else is a 422, not ignored.
    # The description is in the annotation (a Depends() list loses one given on the default).
    sort_by: Annotated[
        Annotated[str, Field(
            pattern="^(name|price|created_at)_(asc|desc)$",
            json_schema_extra={"enum": [f"{name}_{way}" for name in ("name", "price", "created_at") for way in ("asc", "desc")]},
        )] | None,
        Field(description="Sort field and direction in one value, e.g. price_asc or created_at_desc. Without it, by id."),
    ] = None
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

class ProductVariantResponse(ResponseBody, ProductVariantBase):
    id: int
    currency: StoreCurrency
    product_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

class ProductVariantPublic(ResponseBody):
    """A variant as the storefront sees it on a product page: what a customer
    needs to choose and price it. No is_active (only active variants are
    listed), no price_override (price is the effective one) and no timestamps."""
    id: int
    sku: str
    attributes: dict[str, Any]
    price: NonNegativeMoney = Field(
        ..., description="What this variant costs: its own price_override, or the product's price when it has none."
    )
    currency: StoreCurrency
    stock_quantity: Int32Max = Field(..., ge=0, description="Units of this variant in stock.")


class ProductDetailResponse(ProductResponse):
    """GET /products/{id}: the product, plus the variants it is sold by."""
    has_variants: bool = Field(
        ...,
        description=(
            "True when the product has at least one active variant. Such a product is bought "
            "through a variant: add it to the cart with variant_id, a line without one is refused."
        ),
    )
    variants: list[ProductVariantPublic] = Field(
        ..., description="The active variants, by id; empty when has_variants is false."
    )
    price: NonNegativeMoney = Field(
        ...,
        description=(
            "The product's own price, charged when has_variants is false. With variants, each "
            "variant carries the price to show: its price_override, or this value when it has none."
        ),
    )
    stock_quantity: Int32Max = Field(
        ...,
        ge=0,
        description=(
            "The product's own stock, used when has_variants is false. With variants it is not "
            "what can be bought: use each variant's stock_quantity."
        ),
    )


class ProductVariantListResponse(BaseModel):
    """Schema for a list of variants under a product (no pagination - a
    single product's variant count is expected to be small)."""
    items: list[ProductVariantResponse]
    total: int

    model_config = ConfigDict(from_attributes=True)

