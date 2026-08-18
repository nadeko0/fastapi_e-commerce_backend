from datetime import datetime, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from fastapi import status as http_status
from sqlalchemy import func, desc
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.database import get_db
from app.core.security import get_current_admin_user
from app.schemas.product import (
    ProductCreate,
    ProductUpdate,
    ProductInDB,
    ProductResponse,
)
from app.schemas.category import (
    CategoryCreate,
    CategoryResponse,
)
from app.schemas.order import (
    OrderResponse,
    OrderStatus,
    PaymentStatus,
)
from app.schemas.common import APIResponse, PaginationParams
from app.models.user import User
from app.models.product import Product
from app.models.category import Category
from app.models.order import Order
from app.models.order_items import OrderItem
from app.services.redis import RedisService

router = APIRouter(prefix="/admin", tags=["admin"])
@router.post(
    "/categories",
    response_model=APIResponse[CategoryResponse],
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Parent category not found"},
    },
)
async def create_category(
    category: CategoryCreate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):
    """Create a new category, optionally nested under a parent category."""

    if category.parent_id:
        parent = db.query(Category).filter(Category.id == category.parent_id).first()
        if not parent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Parent category not found"
            )

    db_category = Category(**category.dict())
    if category.parent_id:
        parent = db.query(Category).filter(Category.id == category.parent_id).first()
        db_category.level = parent.level + 1
        db_category.path = parent.path + [parent.id]
    else:
        db_category.level = 0
        db_category.path = []

    db.add(db_category)
    db.commit()
    db.refresh(db_category)


    redis.delete("category:tree")

    return APIResponse.success_response(CategoryResponse.from_orm(db_category))

@router.post(
    "/products",
    response_model=APIResponse[ProductResponse],
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Category not found"},
    },
)
async def create_product(
    product: ProductCreate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):
    """Create a new product under an existing category."""

    category = db.query(Category).filter(Category.id == product.category_id).first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )


    db_product = Product(**product.dict())
    db.add(db_product)
    db.commit()
    db.refresh(db_product)


    db_product = db.query(Product).options(
        joinedload(Product.category)
    ).filter(Product.id == db_product.id).first()


    redis.invalidate_category_cache()


    # ProductResponse requires category_name, which is not a Product column,
    # so from_attributes alone can't populate it. Validate the ORM-mapped
    # fields first (ProductInDB has no such requirement), then build the
    # response from that clean field set plus category_name - avoids
    # spreading Product.__dict__ (which includes SQLAlchemy's internal
    # _sa_instance_state) into the Pydantic constructor.
    base = ProductInDB.model_validate(db_product, from_attributes=True)
    response = ProductResponse(**base.model_dump(), category_name=db_product.category.name)
    return APIResponse.success_response(response)

@router.put(
    "/products/{product_id}",
    response_model=APIResponse[ProductResponse],
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product not found"},
    },
)
async def update_product(
    product_id: int,
    product: ProductUpdate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):
    """Update fields on an existing product."""

    db_product = db.query(Product).filter(Product.id == product_id).first()
    if not db_product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )


    for field, value in product.dict(exclude_unset=True).items():
        setattr(db_product, field, value)
    
    db_product.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(db_product)


    db_product = db.query(Product).options(
        joinedload(Product.category)
    ).filter(Product.id == db_product.id).first()


    redis.invalidate_product_cache(product_id)
    redis.invalidate_category_cache()


    base = ProductInDB.model_validate(db_product, from_attributes=True)
    response = ProductResponse(**base.model_dump(), category_name=db_product.category.name)
    return APIResponse.success_response(response)

@router.delete(
    "/products/{product_id}",
    response_model=APIResponse[dict],
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product not found"},
    },
)
async def delete_product(
    product_id: int,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):
    """Delete a product, or soft-delete (deactivate) it if it has existing orders."""

    db_product = db.query(Product).filter(Product.id == product_id).first()
    if not db_product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )


    if db.query(OrderItem).filter(OrderItem.product_id == product_id).count():

        db_product.is_active = False
        db_product.updated_at = datetime.utcnow()
        db.commit()
    else:

        db.delete(db_product)
        db.commit()


    redis.invalidate_product_cache(product_id)
    redis.invalidate_category_cache()

    return APIResponse.success_response({
        "message": "Product deleted successfully"
    })

@router.get(
    "/orders",
    response_model=APIResponse[List[OrderResponse]],
    responses={
        403: {"description": "Admin privileges required"},
        422: {"description": "Invalid status, payment_status, or sort_by value"},
    },
)
async def list_orders(
    # Named "status" for the public API/query param; see the identical note
    # in orders.update_order_status - this shadows the fastapi.status
    # module within this function body, so error responses below must use
    # the http_status alias instead.
    status: Optional[str] = None,
    payment_status: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    pagination: PaginationParams = Depends(),
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """List orders across all users, with filtering, sorting, and pagination."""

    # Eager-load shipping_address (many-to-one) with joinedload and items
    # -> product (one-to-many) with selectinload: OrderResponse serializes
    # both for every order in the page, so without this each row would
    # trigger its own lazy-load queries (N+1) for shipping_address and for
    # each item's product.
    query = db.query(Order).options(
        joinedload(Order.shipping_address),
        selectinload(Order.items).joinedload(OrderItem.product),
    )


    if status:
        try:
            order_status = OrderStatus(status.lower())
            query = query.filter(Order.status == order_status)
        except ValueError:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid order status. Valid values are: {', '.join([s.value for s in OrderStatus])}"
            )
            
    if payment_status:
        try:
            pay_status = PaymentStatus(payment_status.lower())
            query = query.filter(Order.payment_status == pay_status)
        except ValueError:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid payment status. Valid values are: {', '.join([s.value for s in PaymentStatus])}"
            )
    if start_date:
        query = query.filter(Order.created_at >= start_date)
    if end_date:
        query = query.filter(Order.created_at <= end_date)

    # Apply sorting
    if pagination.sort_by:
        try:

            *field_parts, order = pagination.sort_by.rsplit('_', 1)
            field = '_'.join(field_parts)
            

            if not hasattr(Order, field):
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"Invalid sort field: {field}"
                )
            

            if order not in ('asc', 'desc'):
                raise HTTPException(
                    status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Sort order must be either 'asc' or 'desc'"
                )
                
            column = getattr(Order, field)
            query = query.order_by(desc(column) if order == 'desc' else column)
        except ValueError:
            raise HTTPException(
                status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="sort_by must be in format: field_asc or field_desc"
            )
    else:
        query = query.order_by(Order.created_at.desc())

    total = query.count()
    orders = (
        query.offset((pagination.page - 1) * pagination.size)
        .limit(pagination.size)
        .all()
    )

    return APIResponse.success_response([
        OrderResponse.from_orm(order) for order in orders
    ])

@router.get(
    "/stats",
    response_model=APIResponse[dict],
    responses={
        403: {"description": "Admin privileges required"},
        422: {"description": "Invalid period value"},
    },
)
async def get_statistics(
    period: str = Query("24h", regex="^(24h|7d|30d)$"),
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
    redis: RedisService = Depends(),
):
    """Retrieve order/revenue statistics and popular products for a time period."""

    cache_key = f"admin_stats_{period}"
    cached_stats = redis.get(cache_key)
    if cached_stats:
        return APIResponse.success_response(cached_stats)


    now = datetime.utcnow()
    if period == "24h":
        start_date = now - timedelta(hours=24)
    elif period == "7d":
        start_date = now - timedelta(days=7)
    else:  # 30d
        start_date = now - timedelta(days=30)


    base_query = db.query(Order).filter(
        Order.created_at >= start_date,
        Order.status != OrderStatus.CANCELLED,
        Order.payment_status == PaymentStatus.PAID
    )


    stats = {
        "period": period,
        "order_count": base_query.count(),
        "total_revenue": float(base_query.with_entities(
            func.sum(Order.total_amount)
        ).scalar() or 0),
        "average_order_value": float(base_query.with_entities(
            func.avg(Order.total_amount)
        ).scalar() or 0),
    }


    popular_products = (
        db.query(
            Product.id,
            Product.name,
            func.sum(OrderItem.quantity).label('total_quantity'),
            func.sum(OrderItem.quantity * OrderItem.price_at_time).label('total_revenue')
        )
        .join(OrderItem)
        .join(Order)
        .filter(
            Order.created_at >= start_date,
            Order.status != OrderStatus.CANCELLED,
            Order.payment_status == PaymentStatus.PAID
        )
        .group_by(Product.id)
        .order_by(desc('total_quantity'))
        .limit(10)
        .all()
    )

    stats["popular_products"] = [
        {
            "id": p.id,
            "name": p.name,
            "total_quantity": p.total_quantity,
            "total_revenue": float(p.total_revenue)
        }
        for p in popular_products
    ]


    redis.setex(cache_key, 300, stats)

    return APIResponse.success_response(stats)