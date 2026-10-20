from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, Generic, List, Literal, Mapping, Optional, Tuple, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

_ZERO = Decimal("0.00")
_CENT = Decimal("0.01")


class TokenPayload(BaseModel):
    """Schema for JWT token payload"""
    sub: Union[int, str]  # Subject (user ID)
    exp: datetime  # Expiration time
    type: str  # Token type (access, refresh, reset)
DataT = TypeVar('DataT')

class ErrorCode(str, Enum):
    """Standard error codes"""
    VALIDATION_ERROR = "VALIDATION_ERROR"
    NOT_FOUND = "NOT_FOUND"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    CONFLICT = "CONFLICT"
    RATE_LIMIT = "RATE_LIMIT"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    SERVICE_UNAVAILABLE = "SERVICE_UNAVAILABLE"
    DATABASE_ERROR = "DATABASE_ERROR"
    REDIS_ERROR = "REDIS_ERROR"

class ErrorDetail(BaseModel):
    """Detailed error information"""
    code: ErrorCode
    message: str
    field: Optional[str] = None
    details: Optional[Dict[str, Any]] = None

class ValidationError(BaseModel):
    """Validation error details"""
    field: str
    message: str
    code: str

class ValidationErrorResponse(BaseModel):
    """Response for validation errors"""
    errors: List[ValidationError]
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    request_id: Optional[str] = None

class APIResponse(BaseModel, Generic[DataT]):
    """Generic API response wrapper"""
    success: bool = True
    data: Optional[DataT] = None
    error: Optional[ErrorDetail] = None
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    request_id: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def success_response(cls, data: DataT, request_id: Optional[str] = None) -> 'APIResponse[DataT]':
        return cls(success=True, data=data, request_id=request_id)

    @classmethod
    def error_response(
        cls,
        code: ErrorCode,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        request_id: Optional[str] = None
    ) -> 'APIResponse[DataT]':
        error = ErrorDetail(code=code, message=message, details=details)
        return cls(success=False, error=error, request_id=request_id)

class PaginationParams(BaseModel):
    """Common pagination parameters"""
    page: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)
    sort_by: Optional[str] = None
    sort_order: Optional[str] = Field(None, pattern="^(asc|desc)$")

def cart_item_key(product_id: int, variant_id: Optional[int] = None) -> str:
    """The Cart.items dict key for a given product/variant combination.

    A plain product (no variant) keeps the pre-existing "{product_id}" key
    unchanged, so carts written before variant support existed still parse
    and merge exactly as before. A variant line gets its own distinct key
    ("{product_id}:{variant_id}") so two different variants of the same
    product are separate cart lines, never merged with each other or with
    the plain-product line.
    """
    if variant_id is None:
        return str(product_id)
    return f"{product_id}:{variant_id}"

class CartItem(BaseModel):
    """Schema for cart items in Redis"""
    # Without this, gt=0 on quantity was only enforced on construction:
    # Cart.add_item/update_quantity assign to an existing line's quantity,
    # which stored 0 or a negative number unchecked. The cart then failed
    # this same constraint when read back from Redis and was treated as
    # empty, losing every line in it.
    model_config = ConfigDict(validate_assignment=True)

    product_id: int
    variant_id: Optional[int] = None
    quantity: int = Field(..., gt=0)
    added_at: datetime = Field(default_factory=datetime.utcnow)
    # What the customer was shown when the line last went into the cart.
    # A record only: it lets a response say "the price has changed since",
    # and names a product that has since been deleted. Nothing displayed or
    # totalled is read from these three - that comes from the catalog at the
    # moment of the request (CartResponse.from_cart). Optional, because a
    # line is complete without them: product, variant, quantity.
    #
    # The names and the JSON shape are those of the cart format in use
    # before live pricing, where these were the displayed values and the
    # price was a float, so a cart already in Redis loads unchanged. A float
    # read from such a cart becomes the exact two-decimal Decimal it was
    # written from; this code writes the price as a decimal string.
    price_snapshot: Optional[Decimal] = None
    name_snapshot: Optional[str] = None
    image_snapshot: Optional[str] = None

    @field_validator("price_snapshot")
    @classmethod
    def _two_decimal_places(cls, value: Optional[Decimal]) -> Optional[Decimal]:
        # Catalog prices are Numeric(10, 2). An old-format float such as
        # 14.5 arrives as Decimal("14.5"); this makes it print as 14.50.
        if value is None:
            return None
        try:
            return value.quantize(_CENT)
        except InvalidOperation:
            # More digits than a price can have (1E+40, say). Raised as
            # a validation error so a stored cart holding one is handled
            # like any other unreadable cart instead of as a 500.
            raise ValueError("not a valid price")


CART_LINE_PRODUCT_UNAVAILABLE = "product_unavailable"
CART_LINE_VARIANT_UNAVAILABLE = "variant_unavailable"
CART_LINE_INSUFFICIENT_STOCK = "insufficient_stock"


def cart_unit_price(product: Any, variant: Optional[Any]) -> Decimal:
    """The current price of one unit of a cart line: the variant's
    price_override when it has one, otherwise the product's price. The same
    rule create_order (app/api/v1/orders.py) charges by; `is not None`
    because an override of 0.00 is a price, not a missing one."""
    if variant is not None and variant.price_override is not None:
        return variant.price_override
    return product.price


def resolve_cart_line(
    item: CartItem,
    products: Mapping[int, Any],
    variants: Mapping[int, Any],
) -> Tuple[Optional[Any], Optional[Any], Optional[str]]:
    """Match a cart line against the catalog rows loaded for the cart.

    Returns (product, variant, unavailable_reason). The line can be bought
    when the reason is None; these are the conditions create_order checks
    before it takes stock. `product` is returned even when it is inactive,
    so that the line can still be shown under its current name.

    `products` and `variants` are keyed by id and hold anything with the
    attributes of the ORM rows (is_active, price, ...), which keeps this
    module free of the models and the function testable without a database.
    """
    product = products.get(item.product_id)
    if product is None or not product.is_active:
        return product, None, CART_LINE_PRODUCT_UNAVAILABLE
    if item.variant_id is None:
        return product, None, None
    variant = variants.get(item.variant_id)
    if variant is None or variant.product_id != item.product_id or not variant.is_active:
        return product, None, CART_LINE_VARIANT_UNAVAILABLE
    return product, variant, None


class CartResponseItem(BaseModel):
    """One cart line as returned by the API, priced at the moment of the
    request."""
    product_id: int
    variant_id: Optional[int] = None
    quantity: int
    added_at: datetime
    # Current name and main image of the product. For a product that no
    # longer exists, what was recorded when the line was added.
    name: Optional[str] = None
    image: Optional[str] = None
    # False when the product or variant is gone or withdrawn from sale. The
    # line is still listed, so the customer sees what happened to it, but
    # has no price and does not count towards the total.
    available: bool
    unavailable_reason: Optional[
        Literal["product_unavailable", "variant_unavailable"]
    ] = None
    # Current catalog price; None for an unavailable line.
    unit_price: Optional[Decimal] = None
    line_total: Optional[Decimal] = None
    # The price shown when the line was added, and whether the current one
    # differs from it. Information for the customer, never an error.
    price_when_added: Optional[Decimal] = None
    price_changed: bool = False


class CartResponse(BaseModel):
    """A cart as returned by the API. Money is Decimal, serialized as a
    string ("19.98"), like the order total."""
    items: List[CartResponseItem] = Field(default_factory=list)
    # Sum of line_total over the available lines.
    total_amount: Decimal = Field(default=_ZERO)
    # Number of lines, unavailable ones included.
    items_count: int = Field(default=0)
    last_modified: datetime

    @classmethod
    def from_cart(
        cls,
        cart: 'Cart',
        products: Mapping[int, Any],
        variants: Mapping[int, Any],
    ) -> 'CartResponse':
        """Price `cart` from the catalog rows loaded for it (see
        resolve_cart_line for what the two mappings hold). Nothing here
        reads a recorded price except to report that it has changed."""
        items = []
        total_amount = _ZERO
        for item in cart.items.values():
            product, variant, unavailable_reason = resolve_cart_line(item, products, variants)
            if product is not None:
                name = product.name
                image = product.images[0] if product.images else None
            else:
                name = item.name_snapshot
                image = item.image_snapshot or None

            unit_price = line_total = None
            price_changed = False
            if unavailable_reason is None:
                unit_price = cart_unit_price(product, variant)
                line_total = unit_price * item.quantity
                total_amount += line_total
                price_changed = (
                    item.price_snapshot is not None and item.price_snapshot != unit_price
                )

            items.append(CartResponseItem(
                product_id=item.product_id,
                variant_id=item.variant_id,
                quantity=item.quantity,
                added_at=item.added_at,
                name=name,
                image=image,
                available=unavailable_reason is None,
                unavailable_reason=unavailable_reason,
                unit_price=unit_price,
                line_total=line_total,
                price_when_added=item.price_snapshot,
                price_changed=price_changed,
            ))
        return cls(
            items=items,
            total_amount=total_amount,
            items_count=len(items),
            last_modified=cart.updated_at
        )


class CartValidationIssue(BaseModel):
    """A reason the cart cannot be ordered as it stands."""
    product_id: int
    variant_id: Optional[int] = None
    code: Literal["product_unavailable", "variant_unavailable", "insufficient_stock"]
    error: str
    # Set for insufficient_stock only.
    available: Optional[int] = None
    requested: Optional[int] = None


class CartPriceChange(BaseModel):
    """A line whose price differs from the one it was added at."""
    product_id: int
    variant_id: Optional[int] = None
    price_when_added: Decimal
    current_price: Decimal


class CartValidationResponse(BaseModel):
    # True when the cart can be ordered right now: every line is available
    # and in stock. A changed price does not make it False.
    valid: bool
    issues: List[CartValidationIssue] = Field(default_factory=list)
    price_changes: List[CartPriceChange] = Field(default_factory=list)


class Cart(BaseModel):
    """Schema for shopping cart in Redis"""
    # The account the cart belongs to; None for a guest's cart. A guest cart
    # is identified by its Redis key alone (app/schemas/cart.py): the
    # identifier is a secret and is deliberately not repeated in the value.
    # An account cart is stored exactly as before, integer user_id included,
    # so carts already in Redis load and a worker still on the previous code
    # reads what this code writes for an account.
    user_id: Optional[int] = None
    items: Dict[str, CartItem] = Field(default_factory=dict)  # cart_item_key(product_id, variant_id) -> CartItem
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    # When Redis will drop the cart unless it is written again: re-stamped
    # by RedisService on every write, together with the key's TTL.
    expires_at: datetime

    def add_item(
        self,
        product_id: int,
        quantity: int,
        price: Optional[Decimal] = None,
        name: Optional[str] = None,
        image: Optional[str] = None,
        variant_id: Optional[int] = None,
    ) -> None:
        """Add or update item in cart. A given product_id+variant_id pair
        merges quantities; a different variant_id (or no variant_id) for
        the same product_id is a distinct line.

        `price`, `name` and `image` are what the customer is being shown as
        they add the item. Adding more of an existing line records them
        again: the customer has just seen the current price, so that is the
        one a later change is measured against. added_at stays that of the
        first add."""
        str_id = cart_item_key(product_id, variant_id)
        if str_id in self.items:
            line = self.items[str_id]
            # Quantity first: it is the assignment that can be refused (a
            # result of zero or less), and the line must then be untouched.
            line.quantity += quantity
            line.price_snapshot = price
            line.name_snapshot = name
            line.image_snapshot = image
        else:
            self.items[str_id] = CartItem(
                product_id=product_id,
                variant_id=variant_id,
                quantity=quantity,
                price_snapshot=price,
                name_snapshot=name,
                image_snapshot=image
            )
        self.updated_at = datetime.utcnow()

    def remove_item(self, product_id: int, variant_id: Optional[int] = None) -> None:
        """Remove item from cart"""
        str_id = cart_item_key(product_id, variant_id)
        if str_id in self.items:
            del self.items[str_id]
            self.updated_at = datetime.utcnow()

    def update_quantity(self, product_id: int, quantity: int, variant_id: Optional[int] = None) -> None:
        """Update item quantity"""
        str_id = cart_item_key(product_id, variant_id)
        if str_id in self.items:
            self.items[str_id].quantity = quantity
            self.updated_at = datetime.utcnow()

    def clear(self) -> None:
        """Clear all items from cart"""
        self.items.clear()
        self.updated_at = datetime.utcnow()

    def merge(self, other: 'Cart', max_lines: int) -> int:
        """Take the lines of `other` into this cart (a guest's cart into the
        account's, at login). `other` is not changed.

        A line present in both: the quantities are added up, as if the
        customer had added the guest quantity to the account cart. Like an
        add, it re-records the price, name and image - those of the guest
        line, the one the customer has just been looking at - and keeps the
        earlier added_at. Stock is not looked at: this must never be a
        reason to refuse, and a quantity above stock is reported by
        /cart/validate and refused at checkout like any other.

        A line only in `other` is taken as it is, in `other`'s order, while
        this cart has fewer than `max_lines` lines. Returns how many lines
        of `other` did not fit."""
        left_out = 0
        for theirs in other.items.values():
            str_id = cart_item_key(theirs.product_id, theirs.variant_id)
            mine = self.items.get(str_id)
            if mine is not None:
                mine.quantity += theirs.quantity
                mine.price_snapshot = theirs.price_snapshot
                mine.name_snapshot = theirs.name_snapshot
                mine.image_snapshot = theirs.image_snapshot
                mine.added_at = min(mine.added_at, theirs.added_at)
            elif len(self.items) < max_lines:
                self.items[str_id] = theirs.model_copy()
            else:
                left_out += 1
        self.updated_at = datetime.utcnow()
        return left_out

    @property
    def total_items(self) -> int:
        """Get total number of items in cart"""
        return sum(item.quantity for item in self.items.values())

    # There is deliberately no total_amount here. A total computed from the
    # stored lines would be a total at recorded prices, which is what the
    # customer was shown before being charged something else; the only
    # total is CartResponse.from_cart's, priced from the catalog.
