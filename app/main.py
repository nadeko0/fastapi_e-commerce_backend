import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1 import admin, cart, legal, orders, products, users
from app.core import database
from app.core.body_limit import BodySizeLimitMiddleware
from app.core.config import settings
from app.core.database import async_engine, engine
from app.core.errors import register_error_handlers
from app.core.forwarded import ForwardedClientMiddleware
from app.core.logging_config import setup_logging
from app.core.overload import InFlightLimitMiddleware
from app.core.rate_limit import rate_limit
from app.core.security_headers import SecurityHeadersMiddleware, server_error_handler
from app.services.redis import RedisService
from app.services.redis_async import close_async_redis, get_async_redis

setup_logging()
logger = logging.getLogger(__name__)

logger.info("Initializing FastAPI application")

logger.info("Configuring FastAPI middleware, CORS, and rate limiting")

# The interactive docs and the OpenAPI schema describe every route, the
# admin ones included, to anyone who asks. Useful while building against
# the API, not something a production shop needs to publish: there all
# three answer 404 (with openapi_url None, FastAPI serves no docs at all).
_docs_published = settings.ENVIRONMENT != "production"

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Nothing to do at startup. Everything after the yield runs when the
    # server shuts down (lifespan replaces the deprecated on_event
    # "shutdown" handler; same work, same order).
    yield
    logger.info("FastAPI shutdown event triggered")
    cleanup()
    # Not in cleanup(): disposing the async engine has to be awaited.
    await async_engine.dispose()
    # The async Redis client (rate limiter, auth dependencies) belongs to
    # this worker's event loop, which is the loop this handler runs on.
    try:
        await close_async_redis()
        logger.info("Async Redis connections closed successfully")
    except Exception as e:
        logger.error(f"Error closing async Redis connections: {e}")


logger.info("Initializing FastAPI application with rate limiting")
app = FastAPI(
    lifespan=lifespan,
    title=settings.PROJECT_NAME,
    description="E-commerce backend API with FastAPI",
    version=settings.VERSION,
    docs_url=f"{settings.API_V1_STR}/docs" if _docs_published else None,
    redoc_url="/redoc" if _docs_published else None,
    openapi_url=f"{settings.API_V1_STR}/openapi.json" if _docs_published else None,
    # Rate limiting (app/core/rate_limit.py), declared once for every route.
    # A dependency, not a middleware: it runs after routing, so it knows the
    # route template it is counting for, and its 429 is an ordinary
    # HTTPException handled like any other. (As a middleware it sat outside
    # Starlette's ExceptionMiddleware and had to turn its own exception into
    # a response by hand; before that was noticed, every 429 came out as a
    # 500.)
    dependencies=[Depends(rate_limit)],
)

register_error_handlers(app)

# Registered before everything else, so it is the innermost middleware: the
# 413 then passes back out through the security-headers and CORS layers like
# any other response. Nothing outside it reads the body (the rate limiter is
# a route dependency, not a middleware), and the in-flight limiter sits
# outside it, so a request that is shed never gets this far.
app.add_middleware(BodySizeLimitMiddleware, limit=settings.MAX_REQUEST_BODY_BYTES)

logger.debug(f"Configuring CORS with origins: {settings.BACKEND_CORS_ORIGINS}")
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# X-Forwarded-For -> request.client, by TRUSTED_PROXIES and nothing else,
# however the server was started (app/core/forwarded.py).
app.add_middleware(ForwardedClientMiddleware)

# Outside everything but the security headers: a request that is going to be
# shed should cost nothing - no rate-limit Redis call, no CORS handling.
if settings.MAX_IN_FLIGHT_REQUESTS:
    logger.info(f"Limiting in-flight requests to {settings.MAX_IN_FLIGHT_REQUESTS} per worker")
    app.add_middleware(InFlightLimitMiddleware, limit=settings.MAX_IN_FLIGHT_REQUESTS)

# Security headers on every response, the shed 503 and the 500 for an
# unhandled exception included (app/core/security_headers.py). Added after
# the in-flight limit so it is the outermost user middleware.
app.add_middleware(SecurityHeadersMiddleware)
app.add_exception_handler(Exception, server_error_handler)


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
# lifespan "shutdown" phase, which is what runs the lifespan
# handler above. Registering our own signal.signal() handlers here previously
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


async def _database_is_up() -> bool:
    # Through the async session factory, looked up on the module at call
    # time (tests point it at their own database). The synchronous engine
    # used here before blocked the event loop, and with it every other
    # request on this worker, for the whole round trip.
    try:
        async with database.AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except SQLAlchemyError as e:
        logger.error(f"Database health check failed: {e}")
        return False
    return True


async def _redis_is_up() -> bool:
    # The async client, for the same reason (app/services/redis_async.py).
    try:
        await get_async_redis().ping()
    except RedisError as e:
        logger.error(f"Redis health check failed: {e}")
        return False
    return True


@app.get("/health")
async def health_check():
    """Liveness and readiness in one: 200 when the database and Redis both
    answer, 503 otherwise, which is what the Docker healthchecks look at.
    Public and not rate limited, so it says nothing about the deployment:
    no version, no environment, no error text."""
    logger.debug("Health check endpoint called")
    database_up, redis_up = await asyncio.gather(_database_is_up(), _redis_is_up())
    health_status = {
        "status": "healthy" if database_up and redis_up else "degraded",
        "services": {
            "database": "healthy" if database_up else "unhealthy",
            "redis": "healthy" if redis_up else "unhealthy",
        }
    }

    status_code = 200 if health_status["status"] == "healthy" else 503
    logger.info(f"Health check completed with status: {health_status['status']}")
    return JSONResponse(
        content=health_status,
        status_code=status_code
    )
