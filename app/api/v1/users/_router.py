from fastapi import APIRouter

from app.core.openapi import DocumentedRoute

router = APIRouter(prefix="/users", tags=["users"], route_class=DocumentedRoute)
