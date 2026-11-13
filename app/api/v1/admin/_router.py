from fastapi import APIRouter

from app.core.openapi import FORBIDDEN, UNAUTHORIZED, DocumentedRoute

# Every admin operation needs a signed-in administrator. The other error
# responses (413, 422, 500, 503) are added per operation by DocumentedRoute.
router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    route_class=DocumentedRoute,
    responses={401: UNAUTHORIZED, 403: FORBIDDEN},
)
