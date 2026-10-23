"""Annotated aliases for the dependencies routes declare.

`db: DbSession` reads the same as `db: AsyncSession = Depends(...)` and
declares the same dependency; the alias just keeps the callable and its
`scope` in one place (FastAPI: https://fastapi.tiangolo.com/tutorial/dependencies/
"Share Annotated dependencies").

DbSession itself is defined in app.core.database, because app.core.security
(which this module imports) needs it too. Every route and every dependency
that takes a session names the same callable with the same scope, so
FastAPI's per-request dependency cache hands them ONE session.
"""
from typing import Annotated

from fastapi import Depends

from app.core.database import DbSession
from app.core.security import (
    get_current_active_user,
    get_current_admin_user,
    oauth2_scheme,
)
from app.models.user import User
from app.services.payment import PaymentProvider, get_payment_provider
from app.services.redis_async import AsyncRedisService, get_cache_service

__all__ = ["AdminUser", "BearerToken", "Cache", "CurrentUser", "DbSession", "Provider"]

CurrentUser = Annotated[User, Depends(get_current_active_user)]
AdminUser = Annotated[User, Depends(get_current_admin_user)]
Cache = Annotated[AsyncRedisService, Depends(get_cache_service)]
Provider = Annotated[PaymentProvider, Depends(get_payment_provider)]
BearerToken = Annotated[str, Depends(oauth2_scheme)]
