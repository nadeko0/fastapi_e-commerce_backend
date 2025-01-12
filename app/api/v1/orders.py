from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, BackgroundTasks
from sqlalchemy.orm import Session
from decimal import Decimal

from app.core.config import settings
from app.core.database import get_db
from app.core.security import get_current_active_user, get_current_admin_user
from app.schemas.order import (
    OrderCreate,
    OrderResponse,
    OrderUpdate,
    PaymentCreate,
    PaymentResponse,
    OrderStatus,
    PaymentStatus,
)
from app.schemas.common import APIResponse, Cart, PaginationParams
from app.models.user import User
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.product import Product
from app.services.redis import RedisService
from app.services.email import (
    send_order_confirmation_email,
    send_order_status_update_email,
    send_order_cancellation_email,
)

router = APIRouter(prefix="/orders", tags=["orders"])

@router.post("", response_model=APIResponse[OrderResponse])
async def create_order(
    shipping_address_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):

    if not current_user.is_email_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Email verification required before placing orders"
        )
    
    if not current_user.full_name or not current_user.phone:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Complete profile information (full name and phone) required for placing orders"
        )


    cart = redis.get_cart(current_user.id)
    if not cart or not cart.items:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cart is empty"
        )


    address = next(
        (addr for addr in current_user.addresses if addr.id == shipping_address_id),
        None
    )
    if not address:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Shipping address not found"
        )


    order = Order(
        user_id=current_user.id,
        status=OrderStatus.NEW,
        payment_status=PaymentStatus.PENDING,
        shipping_address_id=shipping_address_id,
        total_amount=Decimal('0.00')
    )
    db.add(order)


    total_amount = Decimal('0.00')
    for item in cart.items.values():
        product = db.query(Product).filter(Product.id == item.product_id).first()
        if not product or product.stock_quantity < item.quantity:
            db.rollback()
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Product {item.product_id} not available in requested quantity"
            )


        order_item = OrderItem(
            order_id=order.id,
            product_id=product.id,
            quantity=item.quantity,
            price_at_time=product.price
        )
        db.add(order_item)


        product.stock_quantity -= item.quantity
        total_amount += product.price * item.quantity

    order.total_amount = total_amount
    db.commit()


    redis.delete_cart(current_user.id)


    background_tasks.add_task(
        send_order_confirmation_email,
        current_user.email,
        OrderResponse.from_orm(order)
    )

    return APIResponse.success_response(OrderResponse.from_orm(order))

@router.get("/{order_id}", response_model=APIResponse[OrderResponse])
async def get_order(
    order_id: int,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):

    order = db.query(Order).filter(
        Order.id == order_id,
        Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )
    return APIResponse.success_response(OrderResponse.from_orm(order))

@router.put("/{order_id}/status", response_model=APIResponse[OrderResponse])
async def update_order_status(
    order_id: int,
    status: OrderStatus,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_admin_user),  # Admin only
    db: Session = Depends(get_db),
):

    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )


    if not _is_valid_status_transition(order.status, status):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid status transition"
        )


    old_status = order.status
    order.status = status
    order.updated_at = datetime.utcnow()
    db.commit()


    if status != old_status:
        background_tasks.add_task(
            send_order_status_update_email,
            order.user.email,
            OrderResponse.from_orm(order)
        )

    return APIResponse.success_response(OrderResponse.from_orm(order))

@router.post("/{order_id}/pay", response_model=APIResponse[PaymentResponse])
async def process_payment(
    order_id: int,
    payment: PaymentCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_active_user),
    db: Session = Depends(get_db),
):

    order = db.query(Order).filter(
        Order.id == order_id,
        Order.user_id == current_user.id
    ).first()
    if not order:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Order not found"
        )

    if order.payment_status == PaymentStatus.PAID:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Order already paid"
        )


    payment_response = PaymentResponse(
        **payment.dict(),
        id=1,  # Would be real payment ID
        status=PaymentStatus.PAID,
        created_at=datetime.utcnow(),
        transaction_id="test_transaction"  # Would be real transaction ID
    )


    order.payment_status = PaymentStatus.PAID
    order.status = OrderStatus.CONFIRMED
    order.updated_at = datetime.utcnow()
    db.commit()


    background_tasks.add_task(
        send_order_status_update_email,
        current_user.email,
        OrderResponse.from_orm(order)
    )

    return APIResponse.success_response(payment_response)

def _is_valid_status_transition(old_status: OrderStatus, new_status: OrderStatus) -> bool:
    valid_transitions = {
        OrderStatus.NEW: {OrderStatus.CONFIRMED, OrderStatus.CANCELLED},
        OrderStatus.CONFIRMED: {OrderStatus.PROCESSING, OrderStatus.CANCELLED},
        OrderStatus.PROCESSING: {OrderStatus.SENT, OrderStatus.CANCELLED},
        OrderStatus.SENT: {OrderStatus.DELIVERED, OrderStatus.CANCELLED},
        OrderStatus.DELIVERED: set(),
        OrderStatus.CANCELLED: set(),
    }
    return new_status in valid_transitions.get(old_status, set())