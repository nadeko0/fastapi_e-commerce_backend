from datetime import datetime, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_active_user
from app.schemas.common import APIResponse, Cart, CartItem, CartResponse
from datetime import datetime, timedelta
from app.models.user import User
from app.models.product import Product
from app.services.redis import RedisService

router = APIRouter(prefix="/cart", tags=["cart"])

async def get_cart(
    current_user: User = Depends(get_current_active_user),
    redis: RedisService = Depends(),
) -> Cart:
    cart = redis.get_cart(current_user.id)
    if not cart:
        cart = Cart(
            user_id=current_user.id,
            items={},
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
            expires_at=datetime.utcnow() + timedelta(days=settings.REDIS_CART_TTL_DAYS)
        )
        redis.update_cart(cart)
    return cart

@router.get("", response_model=APIResponse[CartResponse])
async def get_user_cart(
    cart: Cart = Depends(get_cart),
):

    return APIResponse.success_response(CartResponse.from_cart(cart))

@router.post("/items", response_model=APIResponse[CartResponse])
async def add_to_cart(
    product_id: int,
    quantity: int = 1,
    cart: Cart = Depends(get_cart),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    

    str_id = str(product_id)
    current_quantity = cart.items[str_id].quantity if str_id in cart.items else 0
    if product.stock_quantity < (current_quantity + quantity):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not enough stock available"
        )


    cart.add_item(
        product_id=product.id,
        quantity=quantity,
        price=float(product.price),
        name=product.name,
        image=product.images[0] if product.images else ""
    )


    redis.update_cart(cart)

    return APIResponse.success_response(CartResponse.from_cart(cart))

@router.put("/items/{product_id}", response_model=APIResponse[CartResponse])
async def update_cart_item(
    product_id: int,
    quantity: int,
    cart: Cart = Depends(get_cart),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )


    str_id = str(product_id)
    if str_id not in cart.items:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not in cart"
        )


    if product.stock_quantity < quantity:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not enough stock available"
        )


    cart.update_quantity(product_id, quantity)


    redis.update_cart(cart)

    return APIResponse.success_response(CartResponse.from_cart(cart))

@router.delete("/items/{product_id}", response_model=APIResponse[CartResponse])
async def remove_from_cart(
    product_id: int,
    cart: Cart = Depends(get_cart),
    redis: RedisService = Depends(),
):

    str_id = str(product_id)
    if str_id not in cart.items:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not in cart"
        )


    cart.remove_item(product_id)


    redis.update_cart(cart)

    return APIResponse.success_response(CartResponse.from_cart(cart))

@router.delete("", response_model=APIResponse[CartResponse])
async def clear_cart(
    cart: Cart = Depends(get_cart),
    redis: RedisService = Depends(),
):

    cart.clear()
    redis.update_cart(cart)
    return APIResponse.success_response(CartResponse.from_cart(cart))

@router.post("/validate", response_model=APIResponse[dict])
async def validate_cart(
    cart: Cart = Depends(get_cart),
    db: Session = Depends(get_db),
):

    issues = []
    for item in cart.items.values():
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if not product:
            issues.append({
                "product_id": item.product_id,
                "error": "Product no longer available"
            })
        elif product.stock_quantity < item.quantity:
            issues.append({
                "product_id": item.product_id,
                "error": "Not enough stock",
                "available": product.stock_quantity,
                "requested": item.quantity
            })

    return APIResponse.success_response({
        "valid": len(issues) == 0,
        "issues": issues
    })