from fastapi import HTTPException, Request, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.api.merge_patch import PATCH_DESCRIPTION, merge_patch_body
from app.api.v1.admin._router import router
from app.api.v1.products import load_product_detail, product_etag
from app.core.clock import utc_now
from app.core.dependencies import AdminUser, Cache, DbSession
from app.core.etag import IfMatch, require_if_match
from app.core.location import set_location
from app.core.openapi import (
    ETAG_HEADER,
    ETAG_RESPONSE,
    LOCATION_HEADER,
)
from app.models.category import Category
from app.models.order_items import OrderItem
from app.models.product import Product
from app.schemas.category import (
    CategoryCreate,
    CategoryResponse,
)
from app.schemas.common import Int32
from app.schemas.product import (
    ProductCreate,
    ProductInDB,
    ProductResponse,
    ProductUpdate,
)


@router.post(
    "/categories",
    response_model=CategoryResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Category created", "headers": LOCATION_HEADER},
        403: {"description": "Admin privileges required"},
        404: {"description": "Parent category not found"},
    },
)
async def create_category(
    category: CategoryCreate,
    request: Request,
    response: Response,
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
):
    """Create a new category, optionally nested under a parent category.

    Changed in 6.0.0: `metadata` is no longer accepted (it was never stored;
    sending it is a 422), and category responses no longer carry `metadata`,
    `product_count` or `children_count` (always empty or 0 before)."""

    parent = None
    if category.parent_id:
        parent = (
            await db.execute(select(Category).where(Category.id == category.parent_id).limit(1))
        ).scalars().first()
        if not parent:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Parent category not found"
            )

    db_category = Category(**category.model_dump())
    if parent is not None:
        db_category.level = parent.level + 1
        db_category.path = [*parent.path, parent.id]
    else:
        db_category.level = 0
        db_category.path = []

    db.add(db_category)
    # flush, not commit: see create_product.
    await db.flush()
    await db.refresh(db_category)
    result = CategoryResponse.model_validate(db_category)
    set_location(request, response, "get_category", category_id=db_category.id)

    await db.commit()
    await redis.delete("category:tree")
    return result


@router.post(
    "/products",
    response_model=ProductResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Product created", "headers": LOCATION_HEADER},
        403: {"description": "Admin privileges required"},
        404: {"description": "Category not found"},
    },
)
async def create_product(
    product: ProductCreate,
    request: Request,
    response: Response,
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
):
    """Create a new product under an existing category."""

    category = (
        await db.execute(select(Category).where(Category.id == product.category_id).limit(1))
    ).scalars().first()
    if not category:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )


    db_product = Product(**product.model_dump())
    db.add(db_product)
    # flush, not commit: the response is built from the stored row below, and
    # a response that cannot be built must not leave the row behind.
    await db.flush()

    # populate_existing: the product is already in the session, and its
    # category must be loaded by this query (no lazy load under asyncio).
    product_row = (
        await db.execute(
            select(Product)
            .options(joinedload(Product.category))
            .where(Product.id == db_product.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    assert product_row is not None, "Just-flushed product must exist in database"
    db_product = product_row

    # ProductResponse requires category_name, which is not a Product column,
    # so from_attributes alone can't populate it. Validate the ORM-mapped
    # fields first (ProductInDB has no such requirement), then build the
    # response from that clean field set plus category_name - avoids
    # spreading Product.__dict__ (which includes SQLAlchemy's internal
    # _sa_instance_state) into the Pydantic constructor.
    base = ProductInDB.model_validate(db_product, from_attributes=True)
    result = ProductResponse(**base.model_dump(), category_name=db_product.category.name)
    set_location(request, response, "get_product", product_id=db_product.id)

    await db.commit()
    await redis.invalidate_category_cache()
    return result


# PATCH shares the PUT handler: same schema, checks, side effects, response.
@router.patch(
    "/products/{product_id}",
    response_model=ProductResponse,
    summary="Patch Product",
    description=PATCH_DESCRIPTION,
    openapi_extra=merge_patch_body("ProductUpdate"),
    responses={
        200: ETAG_RESPONSE,
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or category not found"},
        412: {"description": "If-Match does not match the product's current ETag (the current one is in the "
                             "ETag header)", "headers": ETAG_HEADER},
    },
)
@router.put(
    "/products/{product_id}",
    response_model=ProductResponse,
    responses={
        200: ETAG_RESPONSE,
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or category not found"},
        412: {"description": "If-Match does not match the product's current ETag (the current one is in the "
                             "ETag header)", "headers": ETAG_HEADER},
    },
)
async def update_product(
    product_id: Int32,
    product: ProductUpdate,
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
    response: Response,
    if_match: IfMatch = None,
):
    """Update fields on an existing product.

    With If-Match (an ETag from GET /products/{id} or a previous update, or
    *), the update is applied only if the product is unchanged since (412
    otherwise). Without it the update is unconditional, as before.

    Every field is optional but at least the ones sent must be valid and
    none may be null; `category_id` must be an existing category (404). A
    product with variants is hashed together with them, so the ETag of
    GET /products/{id} is the one to send. The response carries the new ETag.
    """

    stmt = select(Product).options(joinedload(Product.category)).where(Product.id == product_id).limit(1)
    if if_match is not None:
        # Hold the row until commit so the check and the write are atomic
        # (a no-op on SQLite).
        stmt = stmt.with_for_update(of=Product, key_share=True)
    db_product = (await db.execute(stmt)).scalars().first()
    if not db_product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    if if_match is not None:
        current = ProductResponse(
            **ProductInDB.model_validate(db_product, from_attributes=True).model_dump(),
            category_name=db_product.category.name,
        )
        # The same tag GET /products/{id} returns (hashed with the variants
        # when the product has any).
        require_if_match(if_match, product_etag(current, await load_product_detail(db, current)))

    update_data = product.model_dump(exclude_unset=True)
    # Same check as create_product. Without it a missing category_id was
    # written to the row: a generic 500 from the foreign key on Postgres,
    # and on a database that does not enforce it a committed product whose
    # category does not exist.
    if "category_id" in update_data and not (
        await db.execute(
            select(Category.id).where(Category.id == update_data["category_id"]).limit(1)
        )
    ).first():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    for field, value in update_data.items():
        setattr(db_product, field, value)

    db_product.updated_at = utc_now()
    # flush, not commit: see create_product.
    await db.flush()

    product_row = (
        await db.execute(
            select(Product)
            .options(joinedload(Product.category))
            .where(Product.id == db_product.id)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    assert product_row is not None, "Updated product must exist in database"
    db_product = product_row

    base = ProductInDB.model_validate(db_product, from_attributes=True)
    result = ProductResponse(**base.model_dump(), category_name=db_product.category.name)
    response.headers["ETag"] = product_etag(result, await load_product_detail(db, result))

    await db.commit()
    await redis.invalidate_product_cache(product_id)
    await redis.invalidate_category_cache()
    return result


@router.delete(
    "/products/{product_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses={
        204: {"description": "Product deleted, or deactivated when orders reference it (it is then "
                             "no longer sold and a later GET /products/{id} is a 404)"},
        403: {"description": "Admin privileges required"},
        404: {"description": "Product not found"},
    },
)
async def delete_product(
    product_id: Int32,
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
):
    """Delete a product, or soft-delete (deactivate) it if it has existing orders."""

    db_product = (
        await db.execute(select(Product).where(Product.id == product_id).limit(1))
    ).scalars().first()
    if not db_product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )


    if await db.scalar(
        select(func.count()).select_from(OrderItem).where(OrderItem.product_id == product_id)
    ):

        db_product.is_active = False
        db_product.updated_at = utc_now()
        await db.commit()
    else:

        await db.delete(db_product)
        try:
            await db.commit()
        except IntegrityError:
            # A checkout that held the product's lock while the count ran
            # committed an order line for it: deactivate instead (as above).
            await db.rollback()
            db_product = await db.get(Product, product_id)
            assert db_product is not None, "Product must exist when foreign key IntegrityError occurs"
            db_product.is_active = False
            db_product.updated_at = utc_now()
            await db.commit()


    await redis.invalidate_product_cache(product_id)
    await redis.invalidate_category_cache()
