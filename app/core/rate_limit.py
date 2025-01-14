from typing import Optional, Tuple
from datetime import datetime
import time
from fastapi import Request, HTTPException, Depends
from starlette.status import HTTP_429_TOO_MANY_REQUESTS

from app.services.redis import RedisService
from app.core.config import settings

class RateLimiter:
    def __init__(self):
        self.redis = RedisService()
        # Load rate limits from settings
        self.default_rate_limits = {
            "anonymous": settings.RATE_LIMIT_ANONYMOUS,
            "authenticated": settings.RATE_LIMIT_AUTHENTICATED,
            "admin": settings.RATE_LIMIT_ADMIN,
        }
        # Special endpoints rate limits
        self.endpoint_limits = {
            f"{settings.API_V1_STR}/products": settings.RATE_LIMIT_PRODUCTS,
            f"{settings.API_V1_STR}/auth/login": settings.RATE_LIMIT_LOGIN,
            f"{settings.API_V1_STR}/auth/register": settings.RATE_LIMIT_REGISTER,
        }

    def _get_window_key(self, identifier: str) -> str:
        """Generate Redis key for the rate limit window."""
        return f"ratelimit:{identifier}"

    def _get_client_identifier(self, request: Request) -> Tuple[str, str]:
        """Get client identifier and type based on request."""
        # Get client IP
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0]
        else:
            client_ip = request.client.host if request.client else "unknown"

        # Get user type (anonymous/authenticated/admin)
        user = getattr(request.state, "user", None)
        if user:
            user_type = "admin" if getattr(user, "is_superuser", False) else "authenticated"
        else:
            user_type = "anonymous"

        return client_ip, user_type

    def _get_rate_limit(self, path: str, user_type: str) -> int:
        """Get rate limit based on endpoint and user type."""
        # Check for endpoint-specific limit first
        if path in self.endpoint_limits:
            return self.endpoint_limits[path]
        # Fall back to user type limit
        return self.default_rate_limits.get(user_type, self.default_rate_limits["anonymous"])

    async def check_rate_limit(
        self, request: Request, window_seconds: int = None
    ) -> None:
        """
        Check if request is within rate limits.
        Uses a sliding window algorithm with Redis.
        """
        if not settings.RATE_LIMIT_ENABLED:
            return None

        window_seconds = window_seconds or settings.RATE_LIMIT_WINDOW_SECONDS
        """
        Check if request is within rate limits.
        Uses a sliding window algorithm with Redis.
        """
        # Get client identifier and type
        client_ip, user_type = self._get_client_identifier(request)
        
        # Generate unique key for this client and endpoint
        path = request.url.path
        window_key = self._get_window_key(f"{client_ip}:{path}")
        
        # Get rate limit for this endpoint/user combination
        rate_limit = self._get_rate_limit(path, user_type)
        
        current_time = int(time.time())
        window_start = current_time - window_seconds

        try:
            # Clean old requests and add new request atomically using Redis pipeline
            pipeline = self.redis._redis.pipeline()
            
            # Remove requests older than the window
            pipeline.zremrangebyscore(window_key, "-inf", window_start)
            # Add current request
            pipeline.zadd(window_key, {str(current_time): current_time})
            # Get count of requests in window
            pipeline.zcount(window_key, window_start, "+inf")
            # Set key expiration
            pipeline.expire(window_key, window_seconds)
            
            # Execute pipeline
            _, _, request_count, _ = pipeline.execute()

            # Check if rate limit is exceeded
            if request_count > rate_limit:
                retry_after = window_seconds - (current_time - window_start)
                raise HTTPException(
                    status_code=HTTP_429_TOO_MANY_REQUESTS,
                    detail={
                        "error": "Rate limit exceeded",
                        "retry_after": retry_after,
                        "limit": rate_limit,
                        "window_seconds": window_seconds
                    }
                )

        except Exception as e:
            # Log the error but allow the request in case of Redis failure
            print(f"Rate limiting error: {str(e)}")
            return None

async def rate_limit(request: Request):
    """FastAPI dependency for rate limiting."""
    limiter = RateLimiter()
    await limiter.check_rate_limit(request)
    return True