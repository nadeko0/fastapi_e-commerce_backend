from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from app.core.dependencies import Cache
from app.core.location import set_location
from app.models.order import Order
from app.models.order_items import OrderItem
from app.schemas.order import OrderResponse

# Everything OrderResponse reads off an order: its address, its lines, and
# each line's product and variant (variant_sku). On an AsyncSession a lazy
# load raises MissingGreenlet, so every query whose result is serialized
# carries these options.
_ORDER_RESPONSE_LOADS = (
    joinedload(Order.shipping_address),
    selectinload(Order.items).joinedload(OrderItem.product),
    selectinload(Order.items).joinedload(OrderItem.variant),
)


async def _load_order_for_response(db: AsyncSession, order_id: int) -> Order:
    """The order with everything OrderResponse reads, loaded afresh."""
    return (await db.execute(
        select(Order).options(*_ORDER_RESPONSE_LOADS).where(Order.id == order_id).limit(1)
        .execution_options(populate_existing=True)
    )).scalars().one()


async def _drop_product_details(redis: Cache, product_ids: list[int]) -> None:
    """Drop the cached product details whose stock a committed order change
    moved (see products_with_cached_stock). After the commit, never before:
    a reader between the two would cache the old count again. A Redis error
    is logged by the cache service and not raised - the change is committed
    - so a lost invalidation lasts at most PRODUCT_CACHE_TTL."""
    for product_id in product_ids:
        await redis.invalidate_product_cache(product_id)


async def _order_by_idempotency_key(
    db: AsyncSession, user_id: int, idempotency_key: str
) -> Order | None:
    """The user's order created under this Idempotency-Key, if any."""
    return (await db.execute(
        select(Order).options(*_ORDER_RESPONSE_LOADS).where(
            Order.user_id == user_id,
            Order.idempotency_key == idempotency_key,
        ).limit(1)
    )).scalars().first()


def _created_order(request: Request, response: Response, order: Order) -> OrderResponse:
    """The 201 answer for `order`, the same for the request that created it
    and for a replay of its Idempotency-Key: the order and its Location."""
    set_location(request, response, "get_order", order_id=order.id)
    return OrderResponse.model_validate(order)
