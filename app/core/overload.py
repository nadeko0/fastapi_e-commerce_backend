from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import Problem, ProblemType, problem_response
from app.core.probes import PROBE_PATHS


class InFlightLimitMiddleware:
    """Reject requests with 503 once too many are already being processed.

    Without a bound, overload does not degrade this app, it stops it: every
    request in progress holds pooled database connections until its
    dependencies are torn down, and once more requests are in flight than the
    pool has connections, the event loop blocks waiting for one that only the
    loop itself could release (measured under locust - see README, "Load
    testing"). Nothing is answered until pool_timeout, 30 seconds later.

    Capping in-flight requests below what the pool can serve keeps that from
    ever happening: the request that would have tipped the worker over gets
    an immediate 503 with Retry-After instead, and everyone already inside
    is served at normal speed. An authenticated request can hold two sync
    connections at once (get_current_user's session and the route's own), so
    the safe ceiling is (DB_POOL_SIZE + DB_MAX_OVERFLOW) / 2.

    The count is per worker process, and it covers a request for as long as
    its ASGI call lasts, which includes dependency teardown - the phase the
    stall was hiding in. Deliberately a plain ASGI middleware rather than
    BaseHTTPMiddleware: it has to sit outside everything else and add no
    task or buffering of its own to the requests it lets through.

    The probe paths (/health, /livez, /readyz) are exempt: an orchestrator
    that restarts the container because its health probe got a 503 during
    a traffic peak would turn a brownout
    into an outage.
    """

    def __init__(self, app: ASGIApp, limit: int) -> None:
        self.app = app
        self.limit = limit
        # No lock: only ever touched from the worker's single event loop
        # thread, and never across an await between the check and the update.
        self.in_flight = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in PROBE_PATHS:
            await self.app(scope, receive, send)
            return

        if self.in_flight >= self.limit:
            response = problem_response(scope, Problem(
                503,
                type=ProblemType.SERVICE_OVERLOADED,
                detail="Server is at capacity, please retry shortly",
                retry_after=1,
            ))
            await response(scope, receive, send)
            return

        self.in_flight += 1
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1
