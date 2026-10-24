"""InFlightLimitMiddleware: shed load with a fast 503 instead of letting more
requests in than the database pool can serve (see app/core/overload.py for
the stall this prevents)."""
import asyncio

import pytest
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.requests import ClientDisconnect

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


def _streaming_app(started: asyncio.Event, proceed: asyncio.Event):
    """An app whose response is a stream that is held open until `proceed`."""
    app = FastAPI()

    @app.get("/stream")
    async def stream():
        async def chunks():
            started.set()
            await proceed.wait()
            yield b"late chunk"

        return StreamingResponse(chunks())

    return app


def _request_scope(spec_version: str) -> dict:
    return {
        "type": "http", "path": "/stream", "raw_path": b"/stream", "method": "GET",
        "headers": [], "query_string": b"", "root_path": "", "scheme": "http",
        "server": ("test", 80), "client": ("127.0.0.1", 1234),
        "asgi": {"version": "3.0", "spec_version": spec_version},
    }


def test_slot_is_released_when_the_client_disconnects_mid_response():
    """A slot is held for as long as the ASGI call lasts, so a client that
    goes away while its response is still being produced must give it back
    (Starlette's docs: cleanup belongs in try/finally, which also runs on a
    disconnect or a cancellation). Driven at ASGI level: the request is in
    the handler's stream, the server reports http.disconnect through
    receive(), and the counter must return to what it was. Starlette's
    StreamingResponse listens for the disconnect on ASGI spec versions
    below 2.4."""
    async def scenario():
        started, proceed = asyncio.Event(), asyncio.Event()
        middleware = InFlightLimitMiddleware(_streaming_app(started, proceed), limit=1)
        incoming: asyncio.Queue = asyncio.Queue()
        incoming.put_nowait({"type": "http.request", "body": b"", "more_body": False})
        sent = []

        async def receive():
            return await incoming.get()

        async def send(message):
            sent.append(message)

        before = middleware.in_flight
        call = asyncio.create_task(middleware(_request_scope("2.3"), receive, send))
        await asyncio.wait_for(started.wait(), timeout=2)
        assert middleware.in_flight == before + 1, "the request holds a slot while it streams"

        incoming.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(call, timeout=2)
        return before, middleware.in_flight

    before, after = asyncio.run(scenario())
    assert after == before == 0


def test_slot_is_released_when_sending_to_a_gone_client_fails():
    """The same release on ASGI spec 2.4 and later, where a server signals a
    gone client by making send() raise OSError (Starlette turns that into
    ClientDisconnect) instead of delivering http.disconnect."""
    async def scenario():
        started, proceed = asyncio.Event(), asyncio.Event()
        middleware = InFlightLimitMiddleware(_streaming_app(started, proceed), limit=1)
        gone = False

        async def receive():
            await asyncio.Event().wait()  # no message ever arrives

        async def send(message):
            if gone and message["type"] == "http.response.body":
                raise OSError("client went away")

        call = asyncio.create_task(middleware(_request_scope("2.4"), receive, send))
        await asyncio.wait_for(started.wait(), timeout=2)
        assert middleware.in_flight == 1

        gone = True
        proceed.set()
        with pytest.raises(ClientDisconnect):  # for the server to ignore
            await asyncio.wait_for(call, timeout=2)
        return middleware.in_flight

    assert asyncio.run(scenario()) == 0


def test_slot_is_released_when_the_request_task_is_cancelled():
    async def scenario():
        started, proceed = asyncio.Event(), asyncio.Event()
        middleware = InFlightLimitMiddleware(_streaming_app(started, proceed), limit=1)

        async def receive():
            await asyncio.Event().wait()

        async def send(message):
            pass

        call = asyncio.create_task(middleware(_request_scope("2.3"), receive, send))
        await asyncio.wait_for(started.wait(), timeout=2)
        assert middleware.in_flight == 1

        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        return middleware.in_flight

    assert asyncio.run(scenario()) == 0
