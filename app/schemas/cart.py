# Redis key patterns
#
# The Cart/CartItem/CartResponse Pydantic models formerly defined in this
# module were dead code: app/api/v1/cart.py and app/services/redis.py both
# import their Cart/CartItem/CartResponse from app.schemas.common instead.
# Only these two constants are actually used (by app.services.redis).
CART_KEY_PREFIX = "cart:"
CART_TTL_DAYS = 7  # Cart expiration time in days
