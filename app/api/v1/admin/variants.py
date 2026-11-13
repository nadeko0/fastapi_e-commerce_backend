import logging

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.merge_patch import PATCH_DESCRIPTION, merge_patch_body
from app.api.v1.admin._router import router
from app.core.clock import utc_now
from app.core.dependencies import AdminUser, Cache, DbSession
from app.core.etag import IfMatch, IfNoneMatch, etag_for, if_none_match_hits, require_if_match
from app.core.location import set_location
from app.core.openapi import (
    ETAG_HEADER,
    ETAG_RESPONSE,
    LOCATION_HEADER,
)
from app.models.product import Product, ProductVariant
from app.schemas.common import Int32
from app.schemas.product import (
    ProductVariantCreate,
    ProductVariantListResponse,
    ProductVariantResponse,
    ProductVariantUpdate,
)

# The name the module had before the admin package split: log filters and alerts keep matching.
logger = logging.getLogger("app.api.v1.admin")

# --- Product variants -------------------------------------------------
#
# ProductVariant (app/models/product.py) is additive: a Product with no
# variants still works exactly as before, using its own price/stock_quantity.
# Cart (app/api/v1/cart.py) and checkout (app/api/v1/orders/) are
# variant-aware: an optional variant_id on a cart line prices and checks
# stock against that variant instead of the parent product, and
# decrements/restocks ProductVariant.stock_quantity independently of
# Product.stock_quantity.


async def _get_product_or_404(db: AsyncSession, product_id: int) -> Product:
    product = (
        await db.execute(select(Product).where(Product.id == product_id).limit(1))
    ).scalars().first()
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found"
        )
    return product


async def _variant_sku_exists(db: AsyncSession, sku: str) -> bool:
    """Pre-check only - the real guarantee against a duplicate SKU is the
    unique constraint on ProductVariant.sku, since this read-then-write
    check has a race window between two concurrent requests (see the
    IntegrityError handling in _flush_variant below). Factored out mainly so tests can force the race window (make
    this return False despite a real conflicting row already existing)
    without needing genuine thread interleaving against the test suite's
    single shared SQLite connection."""
    found = await db.execute(select(ProductVariant.id).where(ProductVariant.sku == sku).limit(1))
    return found.first() is not None


async def _flush_variant(db: AsyncSession, sku: str | None, variant_id: int | None = None) -> None:
    """Write a created or updated variant (flush; the route commits once its
    response is built, see create_product), telling a SKU conflict apart
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
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        if sku is not None:
            holder = select(ProductVariant.id).where(ProductVariant.sku == sku)
            if variant_id is not None:
                holder = holder.where(ProductVariant.id != variant_id)
            if (await db.execute(holder.limit(1))).first() is not None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="A variant with this SKU already exists"
                ) from e
        logger.warning("Variant write violated a database constraint", exc_info=e)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="The variant violates a data constraint"
        ) from e


@router.post(
    "/products/{product_id}/variants",
    response_model=ProductVariantResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        201: {"description": "Variant created", "headers": LOCATION_HEADER},
        403: {"description": "Admin privileges required"},
        404: {"description": "Product not found"},
        409: {"description": "SKU already in use"},
        422: {"description": "Invalid variant data"},
    },
)
async def create_product_variant(
    product_id: Int32,
    variant: ProductVariantCreate,
    request: Request,
    response: Response,
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
):
    """Create a new variant (e.g. a size/color combination) under a product.

    The SKU must be unique across all variants (409). Once a product has an
    active variant it is sold through its variants only: the cart and orders
    then need a `variant_id` for it."""

    db_product = await _get_product_or_404(db, product_id)

    if await _variant_sku_exists(db, variant.sku):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A variant with this SKU already exists"
        )

    db_variant = ProductVariant(product_id=product_id, **variant.model_dump())
    db.add(db_variant)
    # The pre-check above is a plain read-then-write: two concurrent
    # requests creating a variant with the same SKU can both pass it before
    # either commits, so the real guarantee is the DB's unique constraint on
    # ProductVariant.sku, not the pre-check. _flush_variant turns the
    # loser's IntegrityError into the same 409 the pre-check path returns
    # for the sequential case, instead of a generic 500.
    # The product's ETag covers its variants through updated_at.
    db_product.updated_at = utc_now()
    await _flush_variant(db, variant.sku)
    await db.refresh(db_variant)
    result = ProductVariantResponse.model_validate(db_variant)
    set_location(
        request, response, "get_product_variant",
        product_id=product_id, variant_id=db_variant.id,
    )

    await db.commit()
    await redis.invalidate_product_cache(product_id)
    return result


@router.get(
    "/products/{product_id}/variants",
    response_model=ProductVariantListResponse,
    responses={
        403: {"description": "Admin privileges required"},
        404: {"description": "Product not found"},
    },
)
async def list_product_variants(
    product_id: Int32,
    # Every route on this router needs it: this one used to answer anyone,
    # inactive variants, SKUs and exact stock levels included.
    current_user: AdminUser,
    db: DbSession,
):
    """List all variants belonging to a product."""

    await _get_product_or_404(db, product_id)

    variants = (
        await db.execute(
            select(ProductVariant)
            .where(ProductVariant.product_id == product_id)
            .order_by(ProductVariant.id)
        )
    ).scalars().all()

    return ProductVariantListResponse(
        items=[ProductVariantResponse.model_validate(v) for v in variants],
        total=len(variants),
    )


@router.get(
    "/products/{product_id}/variants/{variant_id}",
    response_model=ProductVariantResponse,
    responses={
        200: ETAG_RESPONSE,
        304: {"description": "Not modified: If-None-Match matches the variant's current ETag", "headers": ETAG_HEADER},
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or variant not found"},
    },
)
async def get_product_variant(
    product_id: Int32,
    variant_id: Int32,
    current_user: AdminUser,
    db: DbSession,
    response: Response,
    if_none_match: IfNoneMatch = None,
):
    """Retrieve one variant of a product.

    The ETag is computed exactly as on PUT/PATCH (hash of the
    ProductVariantResponse JSON), so it can be sent back as If-Match.
    """

    await _get_product_or_404(db, product_id)

    db_variant = (await db.execute(
        select(ProductVariant)
        .where(ProductVariant.id == variant_id, ProductVariant.product_id == product_id)
        .limit(1)
    )).scalars().first()
    if not db_variant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Variant not found"
        )

    result = ProductVariantResponse.model_validate(db_variant)
    etag = etag_for(result)
    if if_none_match_hits(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    response.headers["ETag"] = etag
    return result


# PATCH shares the PUT handler: same schema, checks, side effects, response.
@router.patch(
    "/products/{product_id}/variants/{variant_id}",
    response_model=ProductVariantResponse,
    summary="Patch Product Variant",
    description=PATCH_DESCRIPTION,
    openapi_extra=merge_patch_body("ProductVariantUpdate"),
    responses={
        200: ETAG_RESPONSE,
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or variant not found"},
        409: {"description": "SKU already in use"},
        412: {"description": "If-Match does not match the variant's current ETag (the current one is in the "
                             "ETag header)", "headers": ETAG_HEADER},
        422: {"description": "Invalid variant data"},
    },
)
@router.put(
    "/products/{product_id}/variants/{variant_id}",
    response_model=ProductVariantResponse,
    responses={
        200: ETAG_RESPONSE,
        403: {"description": "Admin privileges required"},
        404: {"description": "Product or variant not found"},
        409: {"description": "SKU already in use"},
        412: {"description": "If-Match does not match the variant's current ETag (the current one is in the "
                             "ETag header)", "headers": ETAG_HEADER},
        422: {"description": "Invalid variant data"},
    },
)
async def update_product_variant(
    product_id: Int32,
    variant_id: Int32,
    variant: ProductVariantUpdate,
    current_user: AdminUser,
    db: DbSession,
    redis: Cache,
    response: Response,
    if_match: IfMatch = None,
):
    """Update fields on an existing variant of a product.

    With If-Match (the ETag of an earlier update response of this variant, or
    *), applied only if the variant is unchanged since (412 otherwise). The
    SKU must be unique across all variants (409). The response carries the
    new ETag.
    """

    db_product = await _get_product_or_404(db, product_id)

    stmt = (
        select(ProductVariant)
        .where(
            ProductVariant.id == variant_id,
            ProductVariant.product_id == product_id,
        )
        .limit(1)
    )
    if if_match is not None:
        stmt = stmt.with_for_update(key_share=True)
    db_variant = (await db.execute(stmt)).scalars().first()
    if not db_variant:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Variant not found"
        )
    if if_match is not None:
        require_if_match(if_match, etag_for(ProductVariantResponse.model_validate(db_variant)))

    update_data = variant.model_dump(exclude_unset=True)
    if (
        "sku" in update_data
        and update_data["sku"] != db_variant.sku
        and await _variant_sku_exists(db, update_data["sku"])
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A variant with this SKU already exists"
        )

    for field, value in update_data.items():
        setattr(db_variant, field, value)

    db_variant.updated_at = utc_now()
    # The product's ETag covers its variants through updated_at; the cached
    # product detail is dropped after the commit.
    db_product.updated_at = db_variant.updated_at
    # Same read-then-write race as create_product_variant above: the
    # "sku" != db_variant.sku pre-check can be stale by the time this
    # commits if a concurrent request claimed the same SKU in between.
    await _flush_variant(db, update_data.get("sku"), variant_id)
    await db.refresh(db_variant)

    result = ProductVariantResponse.model_validate(db_variant)
    response.headers["ETag"] = etag_for(result)

    await db.commit()
    await redis.invalidate_product_cache(product_id)
    return result
