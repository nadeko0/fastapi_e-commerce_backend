from datetime import datetime
from typing import List, Optional
from pydantic import BaseModel, Field, ConfigDict, validator
from decimal import Decimal

class CartItemBase(BaseModel):
    product_id: int
    quantity: int = Field(gt=0, description="Quantity must be greater than 0")

class CartItemCreate(CartItemBase):
    pass

class CartItemUpdate(BaseModel):
    quantity: int = Field(gt=0, description="Quantity must be greater than 0")

class CartItem(CartItemBase):
    price_at_time: Decimal
    added_at: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(
        json_encoders={Decimal: str},
        from_attributes=True
    )

class Cart(BaseModel):
    user_id: int
    items: List[CartItem] = Field(default_factory=list)
    total_amount: Decimal = Field(default=Decimal('0.00'))
    last_modified: datetime = Field(default_factory=datetime.utcnow)
    
    model_config = ConfigDict(
        json_encoders={Decimal: str},
        from_attributes=True
    )
    
    @validator('total_amount')
    def validate_total_amount(cls, v):
        return Decimal(str(v)).quantize(Decimal('0.01'))

class CartResponse(BaseModel):
    items: List[CartItem]
    total_amount: Decimal
    items_count: int
    
    model_config = ConfigDict(
        json_encoders={Decimal: str},
        from_attributes=True
    )
    
    @property
    def is_empty(self) -> bool:
        return len(self.items) == 0
    
    @validator('total_amount')
    def validate_total_amount(cls, v):
        return Decimal(str(v)).quantize(Decimal('0.01'))
    
    @validator('items_count')
    def set_items_count(cls, v, values):
        return len(values.get('items', []))

# Redis key patterns
CART_KEY_PREFIX = "cart:"
CART_TTL_DAYS = 7  # Cart expiration time in days