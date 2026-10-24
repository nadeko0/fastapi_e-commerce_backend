"""InFlightLimitMiddleware: shed load with a fast 503 instead of letting more
requests in than the database pool can serve (see app/core/overload.py for
the stall this prevents)."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.overload import InFlightLimitMiddleware


def _build(limit: int):
    app = FastAPI()

    @app.get("/work")
    async def work():
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("handler blew up")

    @app.get("/health")
    @app.get("/livez")
    @app.get("/readyz")
    async def health():
        return {"status": "healthy"}

    middleware = InFlightLimitMiddleware(app, limit=limit)
    return middleware, TestClient(middleware, raise_server_exceptions=False)


def test_requests_under_the_limit_pass_through_and_release_their_slot():
    middleware, client = _build(limit=2)

    assert client.get("/work").status_code == 200
    assert client.get("/work").status_code == 200
    assert middleware.in_flight == 0


def test_request_over_the_limit_gets_503_with_retry_after():
    middleware, client = _build(limit=2)
    middleware.in_flight = 2  # two requests already being processed

    response = client.get("/work")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "1"
    assert response.json()["detail"] == "Server is at capacity, please retry shortly"
    # A rejected request never took a slot, so it must not give one back.
    assert middleware.in_flight == 2


@pytest.mark.parametrize("path", ["/health", "/livez", "/readyz"])
def test_probes_are_never_shed(path):
    middleware, client = _build(limit=1)
    middleware.in_flight = 1

    assert client.get(path).status_code == 200
    assert client.get("/work").status_code == 503


def test_slot_is_released_when_the_handler_raises():
    # A leaked slot per failed request would ratchet the worker towards
    # rejecting everything.
    middleware, client = _build(limit=1)

    assert client.get("/boom").status_code == 500
    assert middleware.in_flight == 0
    assert client.get("/work").status_code == 200


def test_concurrent_requests_beyond_the_limit_are_shed_not_queued():
    """The real thing, not a poked counter: hold `limit` requests open inside
    their handlers and check that the next one is answered immediately."""
    release = asyncio.Event()
    entered = 0

    async def slow_app(scope, receive, send):
        nonlocal entered
        entered += 1
        await release.wait()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"done"})

    middleware = InFlightLimitMiddleware(slow_app, limit=2)

    async def call():
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware({"type": "http", "path": "/work", "method": "GET", "headers": []},
                         receive, send)
        return messages[0]["status"]

    async def scenario():
        held = [asyncio.create_task(call()) for _ in range(2)]
        await asyncio.sleep(0)  # let both reach the handler
        assert entered == 2 and middleware.in_flight == 2

        shed = await asyncio.wait_for(call(), timeout=1)  # must not wait for a slot

        release.set()
        return shed, await asyncio.gather(*held)

    shed, served = asyncio.run(scenario())
    assert shed == 503
    assert served == [200, 200]
    assert entered == 2, "the shed request must never reach the application"
    assert middleware.in_flight == 0


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
def test_non_http_scopes_are_passed_straight_through(scope_type):
    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    middleware = InFlightLimitMiddleware(inner, limit=0)
    asyncio.run(middleware({"type": scope_type}, None, None))

    assert seen == [scope_type]
    assert middleware.in_flight == 0
