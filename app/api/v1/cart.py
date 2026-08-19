from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_active_user
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.schemas.common import APIResponse, Cart, CartResponse
from app.services.redis import (
    InsufficientStockError,
    ItemNotInCartError,
    RedisService,
)


def _get_active_variant_for_product(
    db: Session, product_id: int, variant_id: int
) -> ProductVariant:
    """Look up variant_id and validate it belongs to product_id and is
    active. Raises 404 otherwise - a variant_id for a different product,
    an inactive variant, or an unknown id are all "not a valid variant to
    add to cart for this product" from the caller's perspective."""
    variant = db.query(ProductVariant).filter(
        ProductVariant.id == variant_id,
        ProductVariant.product_id == product_id,
        ProductVariant.is_active.is_(True),
    ).first()
    if not variant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product variant not found"
        )
    return variant

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
    """Retrieve the current user's cart."""

    return APIResponse.success_response(CartResponse.from_cart(cart))

@router.post(
    "/items",
    response_model=APIResponse[CartResponse],
    responses={
        400: {"description": "Not enough stock available"},
        404: {"description": "Product not found, or variant_id doesn't belong to product_id / isn't active"},
    },
)
async def add_to_cart(
    product_id: int,
    quantity: int = 1,
    variant_id: Optional[int] = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):
    """Add a product (optionally, a specific variant) to the current user's
    cart, or increase its quantity. A given product_id+variant_id pair
    merges quantities; a different variant_id is tracked as a separate
    cart line."""

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )

    variant = None
    if variant_id is not None:
        variant = _get_active_variant_for_product(db, product_id, variant_id)

    price = variant.price_override if variant and variant.price_override is not None else product.price
    stock_quantity = variant.stock_quantity if variant else product.stock_quantity

    try:
        cart = redis.add_to_cart_atomic(
            user_id=current_user.id,
            product_id=product.id,
            quantity=quantity,
            price=float(price),
            name=product.name,
            image=product.images[0] if product.images else "",
            stock_quantity=stock_quantity,
            variant_id=variant.id if variant else None,
        )
    except InsufficientStockError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not enough stock available"
        )

    return APIResponse.success_response(CartResponse.from_cart(cart))

@router.put(
    "/items/{product_id}",
    response_model=APIResponse[CartResponse],
    responses={
        400: {"description": "Not enough stock available"},
        404: {"description": "Product not found, item not in cart, or variant_id invalid for this product"},
    },
)
async def update_cart_item(
    product_id: int,
    quantity: int,
    variant_id: Optional[int] = None,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):
    """Update the quantity of an item already in the current user's cart."""

    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )

    variant = None
    if variant_id is not None:
        variant = _get_active_variant_for_product(db, product_id, variant_id)
    stock_quantity = variant.stock_quantity if variant else product.stock_quantity

    try:
        cart = redis.update_cart_quantity_atomic(
            user_id=current_user.id,
            product_id=product_id,
            quantity=quantity,
            stock_quantity=stock_quantity,
            variant_id=variant.id if variant else None,
        )
    except ItemNotInCartError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not in cart"
        )
    except InsufficientStockError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Not enough stock available"
        )

    return APIResponse.success_response(CartResponse.from_cart(cart))

@router.delete(
    "/items/{product_id}",
    response_model=APIResponse[CartResponse],
    responses={404: {"description": "Item not in cart"}},
)
async def remove_from_cart(
    product_id: int,
    variant_id: Optional[int] = None,
    current_user: User = Depends(get_current_active_user),
    redis: RedisService = Depends(),
):
    """Remove an item from the current user's cart."""

    try:
        cart = redis.remove_from_cart_atomic(current_user.id, product_id, variant_id=variant_id)
    except ItemNotInCartError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Item not in cart"
        )

    return APIResponse.success_response(CartResponse.from_cart(cart))

@router.delete("", response_model=APIResponse[CartResponse])
async def clear_cart(
    current_user: User = Depends(get_current_active_user),
    redis: RedisService = Depends(),
):
    """Remove all items from the current user's cart."""

    cart = redis.clear_cart_atomic(current_user.id)
    return APIResponse.success_response(CartResponse.from_cart(cart))

@router.post("/validate", response_model=APIResponse[dict])
async def validate_cart(
    cart: Cart = Depends(get_cart),
    db: Session = Depends(get_db),
):
    """Check the current user's cart items against live stock and availability."""

    issues = []
    for item in cart.items.values():
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if not product:
            issues.append({
                "product_id": item.product_id,
                "variant_id": item.variant_id,
                "error": "Product no longer available"
            })
            continue

        if item.variant_id is not None:
            variant = db.query(ProductVariant).filter(
                ProductVariant.id == item.variant_id,
                ProductVariant.product_id == item.product_id,
            ).first()
            if not variant or not variant.is_active:
                issues.append({
                    "product_id": item.product_id,
                    "variant_id": item.variant_id,
                    "error": "Product variant no longer available"
                })
            elif variant.stock_quantity < item.quantity:
                issues.append({
                    "product_id": item.product_id,
                    "variant_id": item.variant_id,
                    "error": "Not enough stock",
                    "available": variant.stock_quantity,
                    "requested": item.quantity
                })
        elif product.stock_quantity < item.quantity:
            issues.append({
                "product_id": item.product_id,
                "variant_id": None,
                "error": "Not enough stock",
                "available": product.stock_quantity,
                "requested": item.quantity
            })

    return APIResponse.success_response({
        "valid": len(issues) == 0,
        "issues": issues
    })
