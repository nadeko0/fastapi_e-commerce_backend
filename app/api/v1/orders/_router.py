from fastapi import APIRouter

from app.core.openapi import DocumentedRoute

router = APIRouter(prefix="/orders", tags=["orders"], route_class=DocumentedRoute)
