import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi import status as http_status
from fastapi.concurrency import run_in_threadpool
from pydantic import Field
from sqlalchemy import desc, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.database import get_db
from app.core.security import get_current_admin_user
from app.models.category import Category
from app.models.order import Order
from app.models.order_items import OrderItem
from app.models.product import Product, ProductVariant
from app.models.user import User
from app.schemas.category import (
    CategoryCreate,
    CategoryResponse,
)
from app.schemas.common import APIResponse, PaginationParams
from app.schemas.order import (
    OrderListResponse,
    OrderResponse,
    OrderStatus,
    PaymentStatus,
)
from app.schemas.product import (
    ProductCreate,
    ProductInDB,
    ProductResponse,
    ProductUpdate,
    ProductVariantCreate,
    ProductVariantListResponse,
    ProductVariantResponse,
    ProductVariantUpdate,
)
from app.services.payment import PaymentProvider, get_payment_provider
from app.services.reconciliation import open_discrepancies, reconcile_payments
from app.services.redis import RedisService
from app.tasks import check_payment_reconciliation

logger = logging.getLogger(__name__)

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
        404: {"description": "Product or category not found"},
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

    update_data = product.dict(exclude_unset=True)
    # Same check as create_product. Without it a missing category_id was
    # written to the row: a generic 500 from the foreign key on Postgres,
    # and on a database that does not enforce it a committed product whose
    # category does not exist.
    if "category_id" in update_data and not (
        db.query(Category.id).filter(Category.id == update_data["category_id"]).first()
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    for field, value in update_data.items():
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

# The columns the admin order list can be sorted by. sort_by used to accept
# any attribute of the Order class (hasattr): relationships such as "user"
# or "items" and SQLAlchemy internals such as "metadata" passed the check
# and then failed in ORDER BY with a 500. Same approach as
# ProductFilter.sort_by: a closed list, enforced by the query schema.
ORDER_SORT_COLUMNS = {
    "id": Order.id,
    "created_at": Order.created_at,
    "updated_at": Order.updated_at,
    "total_amount": Order.total_amount,
    "status": Order.status,
    "payment_status": Order.payment_status,
}


class AdminOrderListParams(PaginationParams):
    sort_by: Optional[str] = Field(
        None, pattern=f"^({'|'.join(ORDER_SORT_COLUMNS)})_(asc|desc)$"
    )


@router.get(
    "/orders",
    response_model=APIResponse[OrderListResponse],
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
    pagination: AdminOrderListParams = Depends(),
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

    # Apply sorting. AdminOrderListParams' pattern has already answered 422
    # for anything but a whitelisted field_asc/field_desc, so both lookups
    # below always succeed.
    if pagination.sort_by:
        field, order = pagination.sort_by.rsplit('_', 1)
        column = ORDER_SORT_COLUMNS[field]
        query = query.order_by(desc(column) if order == 'desc' else column)
    else:
        query = query.order_by(Order.created_at.desc())

    total = query.count()
    orders = (
        query.offset((pagination.page - 1) * pagination.size)
        .limit(pagination.size)
        .all()
    )

    return APIResponse.success_response(OrderListResponse(
        items=[OrderResponse.from_orm(order) for order in orders],
        total=total,
        page=pagination.page,
        size=pagination.size,
        has_more=total > pagination.page * pagination.size,
    ))

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

# --- Product variants -------------------------------------------------
#
# ProductVariant (app/models/product.py) is additive: a Product with no
# variants still works exactly as before, using its own price/stock_quantity.
# Cart (app/api/v1/cart.py) and checkout (app/api/v1/orders.py) are
# variant-aware: an optional variant_id on a cart line prices and checks
# stock against that variant instead of the parent product, and
# decrements/restocks ProductVariant.stock_quantity independently of
# Product.stock_quantity.


def _get_product_or_404(db: Session, product_id: int) -> Product:
    product = db.query(Product).filter(Product.id == product_id).first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    return product


def _variant_sku_exists(db: Session, sku: str) -> bool:
    """Pre-check only - the real guarantee against a duplicate SKU is the
    unique constraint on ProductVariant.sku, since this read-then-write
    check has a race window between two concurrent requests (see the
    IntegrityError handling in _commit_variant below). Factored out mainly so tests can force the race window (make
    this return False despite a real conflicting row already existing)
    without needing genuine thread interleaving against the test suite's
    single shared SQLite connection."""
    return db.query(ProductVariant).filter(ProductVariant.sku == sku).first() is not None


def _commit_variant(db: Session, sku: Optional[str], variant_id: Optional[int] = None) -> None:
    """Commit a created or updated variant, telling a SKU conflict apart
    from any other failed constraint.

    Both variant routes used to answer every IntegrityError with 409 "A
    variant with this SKU already exists", whatever constraint had actually
    failed. The driver's message is not parsed to tell them apart (it
    differs between Postgres and SQLite and between versions); after the
    rollback the question is simply asked again: does another variant now
    hold this SKU? Deliberately not _variant_sku_exists, which tests replace
    to force the race window open. Any other violation is a 422 with a
    generic detail; the database's own message, which names constraints and
    carries the statement's values, is only logged."""
    try:
        db.commit()
    except IntegrityError as e:
        db.rollback()
        if sku is not None:
            holder = db.query(ProductVariant.id).filter(ProductVariant.sku == sku)
            if variant_id is not None:
                holder = holder.filter(ProductVariant.id != variant_id)
            if holder.first() is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A variant with this SKU already exists"
                )
        logger.warning("Variant write violated a database constraint", exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="The variant violates a data constraint"
        )


@router.post(
    "/products/{product_id}/variants",
    response_model=APIResponse[ProductVariantResponse],
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product not found"},
        409: {"description": "SKU already in use"},
        422: {"description": "Invalid variant data"},
    },
)
async def create_product_variant(
    product_id: int,
    variant: ProductVariantCreate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Create a new variant (e.g. a size/color combination) under a product."""

    _get_product_or_404(db, product_id)

    if _variant_sku_exists(db, variant.sku):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A variant with this SKU already exists"
        )

    db_variant = ProductVariant(product_id=product_id, **variant.dict())
    db.add(db_variant)
    # The pre-check above is a plain read-then-write: two concurrent
    # requests creating a variant with the same SKU can both pass it before
    # either commits, so the real guarantee is the DB's unique constraint on
    # ProductVariant.sku, not the pre-check. _commit_variant turns the
    # loser's IntegrityError into the same 409 the pre-check path returns
    # for the sequential case, instead of a generic 500.
    _commit_variant(db, variant.sku)
    db.refresh(db_variant)

    return APIResponse.success_response(ProductVariantResponse.from_orm(db_variant))

@router.get(
    "/products/{product_id}/variants",
    response_model=APIResponse[ProductVariantListResponse],
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product not found"},
    },
)
async def list_product_variants(
    product_id: int,
    # Every route on this router needs it: this one used to answer anyone,
    # inactive variants, SKUs and exact stock levels included.
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """List all variants belonging to a product."""

    _get_product_or_404(db, product_id)

    variants = (
        db.query(ProductVariant)
        .filter(ProductVariant.product_id == product_id)
        .order_by(ProductVariant.id)
        .all()
    )

    return APIResponse.success_response(ProductVariantListResponse(
        items=[ProductVariantResponse.from_orm(v) for v in variants],
        total=len(variants),
    ))

@router.put(
    "/products/{product_id}/variants/{variant_id}",
    response_model=APIResponse[ProductVariantResponse],
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or variant not found"},
        409: {"description": "SKU already in use"},
        422: {"description": "Invalid variant data"},
    },
)
async def update_product_variant(
    product_id: int,
    variant_id: int,
    variant: ProductVariantUpdate,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """Update fields on an existing variant of a product."""

    _get_product_or_404(db, product_id)

    db_variant = db.query(ProductVariant).filter(
        ProductVariant.id == variant_id,
        ProductVariant.product_id == product_id,
    ).first()
    if not db_variant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Variant not found"
        )

    update_data = variant.dict(exclude_unset=True)
    if "sku" in update_data and update_data["sku"] != db_variant.sku:
        if _variant_sku_exists(db, update_data["sku"]):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="A variant with this SKU already exists"
            )

    for field, value in update_data.items():
        setattr(db_variant, field, value)

    db_variant.updated_at = datetime.utcnow()
    # Same read-then-write race as create_product_variant above: the
    # "sku" != db_variant.sku pre-check can be stale by the time this
    # commits if a concurrent request claimed the same SKU in between.
    _commit_variant(db, update_data.get("sku"), variant_id)
    db.refresh(db_variant)

    return APIResponse.success_response(ProductVariantResponse.from_orm(db_variant))


# -- Payment reconciliation ----------------------------------------------------------

@router.get(
    "/reconciliation/discrepancies",
    response_model=APIResponse[dict],
    responses={403: {"description": "Admin privileges required"}},
)
async def list_reconciliation_discrepancies(
    order_id: Optional[int] = None,
    pagination: PaginationParams = Depends(),
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
):
    """The differences the payment reconciliation found between our payments
    and the provider's and that still stand, oldest first (admin only):
    which payment, what kind of difference, what each side says, and since
    when. Optionally those of one order."""
    rows, total = open_discrepancies(
        db, page=pagination.page, size=pagination.size, order_id=order_id
    )
    return APIResponse.success_response({
        "items": [
            {
                "id": d.id,
                "order_id": p.order_id,
                "payment_id": p.id,
                "transaction_id": p.payment_intent_id,
                "kind": d.kind,
                "subject": d.subject,
                "ours": d.ours,
                "theirs": d.theirs,
                "detected_at": d.detected_at,
                "last_seen_at": d.last_seen_at,
            }
            for d, p in rows
        ],
        "total": total,
        "page": pagination.page,
        "size": pagination.size,
        "has_more": total > pagination.page * pagination.size,
    })


@router.post(
    "/reconciliation/run",
    response_model=APIResponse[dict],
    responses={
        202: {"description": "The full run is queued (no order_id)"},
        403: {"description": "Admin privileges required"},
        404: {"description": "Order not found"},
    },
)
async def run_reconciliation(
    response: Response,
    order_id: Optional[int] = None,
    current_user: User = Depends(get_current_admin_user),
    db: Session = Depends(get_db),
    provider: PaymentProvider = Depends(get_payment_provider),
):
    """Reconcile now (admin only). With order_id: every payment of that order,
    whatever its age, and the outcome in the response. Without: the same run
    as the daily one, queued for the worker (202), because it makes up to
    two provider calls per payment."""
    if order_id is None:
        check_payment_reconciliation.delay()
        response.status_code = http_status.HTTP_202_ACCEPTED
        return APIResponse.success_response({"queued": True})
    if not db.query(Order.id).filter(Order.id == order_id).first():
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="Order not found",
        )
    result = await run_in_threadpool(reconcile_payments, db, provider, order_id=order_id)
    return APIResponse.success_response(result.as_dict())
