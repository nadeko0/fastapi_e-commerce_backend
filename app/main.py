import logging

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from starlette.middleware.base import BaseHTTPMiddleware

from app.api.v1 import admin, cart, legal, orders, products, users
from app.core.config import settings
from app.core.database import engine
from app.core.logging_config import setup_logging
from app.core.rate_limit import RateLimiter
from app.services.redis import RedisService

setup_logging()
logger = logging.getLogger(__name__)

logger.info("Initializing FastAPI application")

logger.info("Configuring FastAPI middleware, CORS, and rate limiting")

class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app):
        super().__init__(app)
        self.limiter = RateLimiter()

    async def dispatch(self, request: Request, call_next):
        # Starlette builds its middleware stack as ServerErrorMiddleware ->
        # user middleware (this one included) -> ExceptionMiddleware ->
        # router, so this middleware sits *outside* ExceptionMiddleware.
        # An HTTPException raised here (e.g. the 429 from check_rate_limit)
        # is therefore never converted to its intended status/body by
        # ExceptionMiddleware - it falls through to ServerErrorMiddleware
        # and becomes a generic, unhelpful 500. That silently defeated the
        # login rate limiter's 429 response in production: exceeding the
        # limit returned "500 Internal Server Error" instead of 429 with
        # the retry_after/limit detail. Catch it here and build the
        # response ourselves.
        try:
            await self.limiter.check_rate_limit(request)
        except HTTPException as exc:
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.detail},
                headers=exc.headers,
            )
        return await call_next(request)
logger.info("Initializing FastAPI application with rate limiting")
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="E-commerce backend API with FastAPI",
    version=settings.VERSION,
    docs_url=f"{settings.API_V1_STR}/docs",
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

logger.debug("Adding rate limiting middleware")
app.add_middleware(RateLimitMiddleware)

logger.debug("Adding security headers middleware")
@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self';"
        "img-src 'self' data: https:;"
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net;"
        "font-src 'self' https://cdn.jsdelivr.net;"
    )
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=()"
    return response

logger.debug(f"Configuring CORS with origins: {settings.BACKEND_CORS_ORIGINS}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


logger.info("Initializing service connections")
redis_service = RedisService()
logger.info("Redis service initialized")

def cleanup():
    logger.info("Starting graceful shutdown...")


    try:
        logger.info("Closing database connections...")
        engine.dispose()
        logger.info("Database connections closed successfully")
    except Exception as e:
        logger.error(f"Error closing database connections: {e}")


    global redis_service
    if redis_service:
        try:
            logger.info("Closing Redis connections...")
            redis_service._redis.close()
            logger.info("Redis connections closed successfully")
        except Exception as e:
            logger.error(f"Error closing Redis connections: {e}")

    logger.info("Graceful shutdown completed")

# No custom SIGINT/SIGTERM handlers here: uvicorn's own Server already
# installs signal handlers for both signals and performs a graceful
# shutdown - it stops accepting new connections, lets in-flight requests
# finish (up to its graceful-shutdown timeout), *then* runs the ASGI
# lifespan "shutdown" phase, which is what triggers the shutdown_event
# below. Registering our own signal.signal() handlers here previously
# raced with that: our handler called sys.exit(0) as soon as cleanup()
# returned, with no regard for requests still in flight, and could run
# before or instead of uvicorn's own handler depending on import timing.
# That's strictly worse than doing nothing and letting uvicorn's handling
# (which this app already relies on via the shutdown event) do its job.
app.include_router(users.router, prefix=settings.API_V1_STR)
app.include_router(products.router, prefix=settings.API_V1_STR)
app.include_router(orders.router, prefix=settings.API_V1_STR)
app.include_router(cart.router, prefix=settings.API_V1_STR)
app.include_router(admin.router, prefix=settings.API_V1_STR)
app.include_router(legal.router, prefix=settings.API_V1_STR)


@app.on_event("shutdown")
async def shutdown_event():

    logger.info("FastAPI shutdown event triggered")
    cleanup()

@app.get("/health")
async def health_check():
    logger.debug("Health check endpoint called")
    health_status = {
        "status": "healthy",
        "version": settings.VERSION,
        "environment": settings.ENVIRONMENT,
        "services": {
            "database": "healthy",
            "redis": "healthy"
        }
    }


    logger.debug("Checking database connection")
    try:
        with engine.connect() as connection:
            result = connection.execute(text("SELECT 1"))
            result.scalar()  # Fetch the result
    except SQLAlchemyError as e:
        logger.error(f"Database health check failed: {e}")
        health_status["services"]["database"] = "unhealthy"
        health_status["status"] = "degraded"


    logger.debug("Checking Redis connection")
    try:
        global redis_service
        redis_service._redis.ping()
    except RedisError as e:
        logger.error(f"Redis health check failed: {e}")
        health_status["services"]["redis"] = "unhealthy"
        health_status["status"] = "degraded"

    status_code = 200 if health_status["status"] == "healthy" else 503
    logger.info(f"Health check completed with status: {health_status['status']}")
    return JSONResponse(
        content=health_status,
        status_code=status_code
    )
