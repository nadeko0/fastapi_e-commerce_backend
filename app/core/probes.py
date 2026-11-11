"""Health probes: /livez, /readyz, and the checks behind /health.

Follows the Kubernetes convention for the three probe kinds
(https://kubernetes.io/docs/concepts/configuration/liveness-readiness-startup-probes/):

- /livez: the process is up and serving. No dependency is touched, because
  a liveness failure restarts the container and restarting every pod
  because Redis blinked would turn a dependency blip into an outage.
- /readyz: the dependencies this instance needs (database, Redis) answer
  within a short timeout. A failure takes the instance out of rotation
  (503 with Retry-After) without restarting it.
- /health: the original combined probe, kept byte for byte for existing
  clients and the Docker healthchecks.

The body format is deliberately not application/health+json
(draft-inadarei-api-health-check): that draft expired in 2021 and never
became a standard, so a plain small JSON object is used. The probes are
public, so bodies carry no version, environment or error text.

All three answer GET and HEAD, and are never cached (RFC 9111 5.2.2.5,
no-store): a cached 200 would hide an outage from the monitor.
"""

import asyncio
import logging
from typing import Any, Literal

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core import database
from app.services.redis_async import get_async_redis

logger = logging.getLogger(__name__)

# A dependency that has not answered in this long is not ready: a probe
# must itself answer well inside the orchestrator's own probe timeout.
READINESS_TIMEOUT_SECONDS = 2.0
RETRY_AFTER_SECONDS = 5

# Exempt from rate limiting and the in-flight limit: a probe answered 429/503
# under load gets the container restarted or pulled from rotation.
PROBE_PATHS = frozenset({"/health", "/livez", "/readyz"})

NO_STORE = {"Cache-Control": "no-store"}

router = APIRouter(tags=["health"])


# What the probes answer, for the OpenAPI document only: the routes return a
# ready-made response, so nothing validates against these.
class LivenessBody(BaseModel):
    status: Literal["ok"]


class ReadinessChecks(BaseModel):
    database: Literal["ok", "fail"]
    redis: Literal["ok", "fail"]


class ReadinessBody(BaseModel):
    status: Literal["ok", "fail"]
    checks: ReadinessChecks


class HealthServices(BaseModel):
    database: Literal["healthy", "unhealthy"]
    redis: Literal["healthy", "unhealthy"]


class HealthBody(BaseModel):
    status: Literal["healthy", "degraded"]
    services: HealthServices


_PROBE_HEADERS = {"Cache-Control": {"description": "Always no-store.", "schema": {"type": "string"}}}


def probe_responses(model: type[BaseModel]) -> dict[int | str, dict[str, Any]]:
    """The `responses=` of a probe that can fail: the 200 and the 503 carry
    the same shape, the 503 with Retry-After."""
    return {
        200: {"description": "Ready", "model": model, "headers": _PROBE_HEADERS},
        503: {
            "description": "Not ready",
            "model": model,
            "headers": {
                **_PROBE_HEADERS,
                "Retry-After": {
                    "description": "Seconds to wait before probing again.",
                    "schema": {"type": "integer", "minimum": 0},
                },
            },
        },
    }


async def database_is_up(limit_seconds: float | None = None) -> bool:
    # Through the async session factory, looked up on the module at call
    # time (tests point it at their own database). The synchronous engine
    # used here before blocked the event loop, and with it every other
    # request on this worker, for the whole round trip.
    try:
        async with asyncio.timeout(limit_seconds), database.AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError, TimeoutError) as e:
        logger.error("Database health check failed: %r", e)
        return False
    return True


async def redis_is_up(limit_seconds: float | None = None) -> bool:
    # The async client, for the same reason (app/services/redis_async.py).
    try:
        async with asyncio.timeout(limit_seconds):
            await get_async_redis().ping()
    except (RedisError, OSError, TimeoutError) as e:
        logger.error("Redis health check failed: %r", e)
        return False
    return True


def probe_response(request: Request, content: dict[str, Any], status_code: int = 200, headers: dict[str, Any] | None = None) -> Response:
    """JSON for GET; the same status and headers and no body for HEAD."""
    headers = {**NO_STORE, **(headers or {})}
    if request.method == "HEAD":
        return Response(status_code=status_code, headers=headers, media_type="application/json")
    return JSONResponse(content, status_code=status_code, headers=headers)


# HEAD is served but kept out of the schema: one documented operation per
# probe, and FastAPI would give a GET+HEAD route two identical operation ids.
@router.head("/livez", include_in_schema=False)
@router.get("/livez", responses={200: {"description": "Alive", "model": LivenessBody, "headers": _PROBE_HEADERS}})
async def liveness(request: Request):
    """The process is up and serving; checks nothing else. Public, never
    rate limited or cached."""
    return probe_response(request, {"status": "ok"})


@router.head("/readyz", include_in_schema=False)
@router.get("/readyz", responses=probe_responses(ReadinessBody))
async def readiness(request: Request):
    """200 when the database and Redis both answer within a short timeout
    (2 seconds each), 503 with Retry-After: 5 otherwise. Public, never rate
    limited or cached."""
    database_up, redis_up = await asyncio.gather(
        database_is_up(READINESS_TIMEOUT_SECONDS), redis_is_up(READINESS_TIMEOUT_SECONDS)
    )
    ready = database_up and redis_up
    body = {
        "status": "ok" if ready else "fail",
        "checks": {
            "database": "ok" if database_up else "fail",
            "redis": "ok" if redis_up else "fail",
        },
    }
    if ready:
        return probe_response(request, body)
    return probe_response(request, body, 503, {"Retry-After": str(RETRY_AFTER_SECONDS)})
