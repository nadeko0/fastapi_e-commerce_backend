from datetime import datetime
from typing import Optional, Any, Generic, TypeVar, Dict, List, Union
from pydantic import BaseModel, Field, ConfigDict
from enum import Enum
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

class ErrorResponse(BaseModel):
    """Standard error response"""
    error: ErrorDetail
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    request_id: Optional[str] = None

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

class CartItem(BaseModel):
    """Schema for cart items in Redis"""
    product_id: int
    quantity: int = Field(..., gt=0)
    added_at: datetime = Field(default_factory=datetime.utcnow)
    price_snapshot: float  # Current price when added
    name_snapshot: str  # Product name when added
    image_snapshot: str  # Main product image when added

class CartResponseItem(BaseModel):
    """Schema for cart item in API response"""
    product_id: int
    quantity: int
    price_at_time: float
    added_at: datetime
    name: str
    image: str

class CartResponse(BaseModel):
    """Schema for cart in API response"""
    items: List[CartResponseItem] = Field(default_factory=list)
    total_amount: float = Field(default=0.0)
    items_count: int = Field(default=0)
    last_modified: datetime

    @classmethod
    def from_cart(cls, cart: 'Cart') -> 'CartResponse':
        """Convert internal Cart model to CartResponse"""
        items = [
            CartResponseItem(
                product_id=item.product_id,
                quantity=item.quantity,
                price_at_time=item.price_snapshot,
                added_at=item.added_at,
                name=item.name_snapshot,
                image=item.image_snapshot
            )
            for item in cart.items.values()
        ]
        return cls(
            items=items,
            total_amount=sum(item.quantity * item.price_at_time for item in items),
            items_count=len(items),
            last_modified=cart.updated_at
        )

class Cart(BaseModel):
    """Schema for shopping cart in Redis"""
    user_id: int
    items: Dict[str, CartItem] = Field(default_factory=dict)  # product_id -> CartItem
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime  # TTL tracking

    def add_item(self, product_id: int, quantity: int, price: float, name: str, image: str) -> None:
        """Add or update item in cart"""
        str_id = str(product_id)
        if str_id in self.items:
            self.items[str_id].quantity += quantity
        else:
            self.items[str_id] = CartItem(
                product_id=product_id,
                quantity=quantity,
                price_snapshot=price,
                name_snapshot=name,
                image_snapshot=image
            )
        self.updated_at = datetime.utcnow()

    def remove_item(self, product_id: int) -> None:
        """Remove item from cart"""
        str_id = str(product_id)
        if str_id in self.items:
            del self.items[str_id]
            self.updated_at = datetime.utcnow()

    def update_quantity(self, product_id: int, quantity: int) -> None:
        """Update item quantity"""
        str_id = str(product_id)
        if str_id in self.items:
            self.items[str_id].quantity = quantity
            self.updated_at = datetime.utcnow()

    def clear(self) -> None:
        """Clear all items from cart"""
        self.items.clear()
        self.updated_at = datetime.utcnow()

    @property
    def total_items(self) -> int:
        """Get total number of items in cart"""
        return sum(item.quantity for item in self.items.values())

    @property
    def total_amount(self) -> float:
        """Calculate total cart amount"""
        return sum(item.quantity * item.price_snapshot for item in self.items.values())