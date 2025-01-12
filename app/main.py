import logging
import signal
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from redis.exceptions import RedisError

from app.core.config import settings
from app.core.database import engine
from app.services.redis import RedisService
from app.core.logging_config import setup_logging
from app.api.v1 import users, products, orders, cart, admin

setup_logging()
logger = logging.getLogger(__name__)

logger.info("Initializing FastAPI application")

logger.info("Configuring FastAPI middleware and CORS")
app = FastAPI(
    title=settings.PROJECT_NAME,
    description="E-commerce backend API with FastAPI",
    version=settings.VERSION,
    docs_url=f"{settings.API_V1_STR}/docs",
    openapi_url=f"{settings.API_V1_STR}/openapi.json"
)

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

def signal_handler(signum, frame):
    logger.info(f"Received signal {signum}")
    cleanup()
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)
app.include_router(users.router, prefix=settings.API_V1_STR)
app.include_router(products.router, prefix=settings.API_V1_STR)
app.include_router(orders.router, prefix=settings.API_V1_STR)
app.include_router(cart.router, prefix=settings.API_V1_STR)
app.include_router(admin.router, prefix=settings.API_V1_STR)


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
        health_status["services"]["database"] = f"unhealthy: {str(e)}"
        health_status["status"] = "degraded"
    

    logger.debug("Checking Redis connection")
    try:
        global redis_service
        redis_service._redis.ping()
    except RedisError as e:
        health_status["services"]["redis"] = f"unhealthy: {str(e)}"
        health_status["status"] = "degraded"
    
    status_code = 200 if health_status["status"] == "healthy" else 503
    logger.info(f"Health check completed with status: {health_status['status']}")
    return JSONResponse(
        content=health_status,
        status_code=status_code
    )