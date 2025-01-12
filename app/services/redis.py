import json
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, TYPE_CHECKING

if TYPE_CHECKING:
    from app.schemas.category import CategoryTreeResponse
from redis import Redis, ConnectionPool, ConnectionError
from decimal import Decimal

from app.core.config import settings
from app.schemas.common import Cart, CartItem
from app.schemas.cart import CART_KEY_PREFIX, CART_TTL_DAYS

class RedisService:
    _instance = None
    _pool = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._pool = ConnectionPool(
                host=settings.REDIS_HOST,
                port=settings.REDIS_PORT,
                db=settings.REDIS_DB,
                decode_responses=True,
                max_connections=50
            )
        return cls._instance

    def __init__(self):
        self._redis: Redis = Redis(connection_pool=self._pool)

    def _get_cart_key(self, user_id: int) -> str:
        return f"{CART_KEY_PREFIX}{user_id}"

    def _serialize(self, data: Any) -> str:
        if isinstance(data, (Cart, CartItem)) or hasattr(data, 'model_dump'):
            return json.dumps(data.model_dump(mode='json'), default=str)
        return json.dumps(data, default=str)

    def _deserialize(self, data: str, model_class=None) -> Any:
        if not data:
            return None
            
        try:
            parsed = json.loads(data)
            if model_class:

                if model_class.__name__ == 'Cart':
                    if 'items' not in parsed:
                        parsed['items'] = {}
                    elif not isinstance(parsed['items'], dict):

                        items_dict = {}
                        for item in parsed['items']:
                            items_dict[str(item['product_id'])] = item
                        parsed['items'] = items_dict
                    

                    if 'expires_at' not in parsed:
                        parsed['expires_at'] = (datetime.utcnow() + timedelta(days=CART_TTL_DAYS)).isoformat()
                

                if model_class.__name__ == 'CategoryTreeResponse':
                    if isinstance(parsed, str):
                        parsed = {'tree': [], 'total_categories': 0, 'max_depth': 0}
                    elif not isinstance(parsed, dict):
                        parsed = parsed.dict() if hasattr(parsed, 'dict') else {'tree': [], 'total_categories': 0, 'max_depth': 0}
                return model_class.model_validate(parsed)
            return parsed
        except (json.JSONDecodeError, AttributeError) as e:
            print(f"Deserialization error: {str(e)}")
            return None

    def _handle_redis_error(self, operation: str) -> None:
        print(f"Redis operation failed: {operation}")


    def get_cart(self, user_id: int) -> Optional[Cart]:
        try:
            cart_data = self._redis.get(self._get_cart_key(user_id))
            if cart_data:
                return self._deserialize(cart_data, Cart)
            return None
        except ConnectionError as e:
            self._handle_redis_error(f"get_cart: {str(e)}")
            return None

    def update_cart(self, cart: Cart) -> bool:
        try:
            cart_key = self._get_cart_key(cart.user_id)
            return self._redis.setex(
                cart_key,
                timedelta(days=CART_TTL_DAYS),
                self._serialize(cart)
            )
        except ConnectionError as e:
            self._handle_redis_error(f"update_cart: {str(e)}")
            return False

    def delete_cart(self, user_id: int) -> bool:
        try:
            return bool(self._redis.delete(self._get_cart_key(user_id)))
        except ConnectionError as e:
            self._handle_redis_error(f"delete_cart: {str(e)}")
            return False


    def add_to_blacklist(self, token: str, expires_in: int) -> bool:
        try:
            key = f"blacklist:{token}"
            return self._redis.setex(key, expires_in, "1")
        except ConnectionError as e:
            self._handle_redis_error(f"add_to_blacklist: {str(e)}")
            return False

    def is_blacklisted(self, token: str) -> bool:
        try:
            return bool(self._redis.exists(f"blacklist:{token}"))
        except ConnectionError as e:
            self._handle_redis_error(f"is_blacklisted: {str(e)}")
            return True  # Safer to assume token is blacklisted on error


    def cache_product(self, product_id: int, data: Dict) -> bool:
        try:
            key = f"product:{product_id}"
            return self._redis.setex(
                key,
                timedelta(hours=1),  # 1 hour cache as per requirements
                self._serialize(data)
            )
        except ConnectionError as e:
            self._handle_redis_error(f"cache_product: {str(e)}")
            return False

    def cache_category_tree(self, tree_data: 'CategoryTreeResponse') -> bool:
        try:
            serialized = self._serialize(tree_data)

            result = self._redis.setex(
                "category:tree",
                timedelta(hours=settings.REDIS_PRODUCT_CACHE_TTL_HOURS),
                serialized
            )

            if not result:
                self._redis.delete("category:tree")
            return result
        except ConnectionError as e:
            self._handle_redis_error(f"cache_category_tree: {str(e)}")
            return False

    def get_cached_category_tree(self) -> Optional['CategoryTreeResponse']:
        try:
            from app.schemas.category import CategoryTreeResponse
            data = self._redis.get("category:tree")
            if not data:
                return None
            

            try:
                parsed = json.loads(data)
                if not isinstance(parsed, dict):
                    self._redis.delete("category:tree")
                    return None
            except json.JSONDecodeError:
                self._redis.delete("category:tree")
                return None
                
            return self._deserialize(data, CategoryTreeResponse)
        except ConnectionError as e:
            self._handle_redis_error(f"get_cached_category_tree: {str(e)}")
            return None

    def get_cached_product(self, product_id: int) -> Optional[Dict]:
        try:
            data = self._redis.get(f"product:{product_id}")
            return self._deserialize(data) if data else None
        except ConnectionError as e:
            self._handle_redis_error(f"get_cached_product: {str(e)}")
            return None


    def create_session(self, user_id: int, session_data: Dict) -> str:
        try:
            session_id = f"session:{user_id}:{datetime.utcnow().timestamp()}"
            self._redis.setex(
                session_id,
                timedelta(hours=24),
                self._serialize(session_data)
            )
            return session_id
        except ConnectionError as e:
            self._handle_redis_error(f"create_session: {str(e)}")
            return ""

    def get_session(self, session_id: str) -> Optional[Dict]:
        try:
            data = self._redis.get(session_id)
            return self._deserialize(data) if data else None
        except ConnectionError as e:
            self._handle_redis_error(f"get_session: {str(e)}")
            return None

    def delete_session(self, session_id: str) -> bool:
        try:
            return bool(self._redis.delete(session_id))
        except ConnectionError as e:
            self._handle_redis_error(f"delete_session: {str(e)}")
            return False

    def cleanup_expired_carts(self) -> int:
        try:
            pattern = f"{CART_KEY_PREFIX}*"
            cleaned = 0
            for key in self._redis.scan_iter(pattern):
                if not self._redis.ttl(key):
                    self._redis.delete(key)
                    cleaned += 1
            return cleaned
        except ConnectionError as e:
            self._handle_redis_error(f"cleanup_expired_carts: {str(e)}")
            return 0

    def delete(self, key: str) -> bool:
        try:
            return bool(self._redis.delete(key))
        except ConnectionError as e:
            self._handle_redis_error(f"delete: {str(e)}")
            return False

    def invalidate_category_cache(self) -> bool:
        try:
            return self.delete("category:tree")
        except ConnectionError as e:
            self._handle_redis_error(f"invalidate_category_cache: {str(e)}")
            return False

    def invalidate_product_cache(self, product_id: int) -> bool:
        try:
            return self.delete(f"product:{product_id}")
        except ConnectionError as e:
            self._handle_redis_error(f"invalidate_product_cache: {str(e)}")
            return False

    def get(self, key: str) -> Optional[Any]:
        try:
            data = self._redis.get(key)
            return self._deserialize(data) if data else None
        except ConnectionError as e:
            self._handle_redis_error(f"get: {str(e)}")
            return None

    def setex(self, key: str, seconds: int, value: Any) -> bool:
        try:
            return self._redis.setex(key, seconds, self._serialize(value))
        except ConnectionError as e:
            self._handle_redis_error(f"setex: {str(e)}")
            return False