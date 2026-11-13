from app.api.v1.users._router import router

from app.api.v1.users import auth, profile, addresses, orders, password  # isort: skip
from app.api.v1.users.addresses import ADDRESS_CONFLICT, DEFAULT_CONFLICT

__all__ = [
    "ADDRESS_CONFLICT",
    "DEFAULT_CONFLICT",
    "addresses",
    "auth",
    "orders",
    "password",
    "profile",
    "router",
]
