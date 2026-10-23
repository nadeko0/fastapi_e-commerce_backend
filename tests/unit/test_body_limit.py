"""BodySizeLimitMiddleware: 413 for an oversized body, by Content-Length or
by counting chunks, and never a truncated body handed to the app (see
app/core/body_limit.py)."""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.body_limit import BodySizeLimitMiddleware

LIMIT = 100
TOO_LARGE = {"detail": "Request body is too large"}


def _build():
    app = FastAPI()
    calls = []

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        calls.append(1)  # only a body read to its end gets here
        return {"size": len(body)}

    @app.post("/boom")
    async def boom(request: Request):
        raise RuntimeError("handler blew up")

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return TestClient(BodySizeLimitMiddleware(app, limit=LIMIT), raise_server_exceptions=False), calls


def test_content_length_over_the_limit_is_413_and_the_app_is_not_called():
    client, calls = _build()

    response = client.post("/echo", content=b"x" * (LIMIT + 1))

    assert response.status_code == 413
    assert response.json() == TOO_LARGE
    assert calls == []


def test_body_at_the_limit_and_just_under_it_are_handled_normally():
    client, _ = _build()

    assert client.post("/echo", content=b"x" * LIMIT).json() == {"size": LIMIT}
    assert client.post("/echo", content=b"x" * (LIMIT - 1)).json() == {"size": LIMIT - 1}


def test_chunked_body_over_the_limit_is_413_and_the_app_never_sees_a_partial_body():
    client, calls = _build()

    def chunks():
        for _ in range(5):
            yield b"x" * 30  # 150 bytes in total, no Content-Length

    response = client.post("/echo", content=chunks())

    assert response.status_code == 413
    assert response.json() == TOO_LARGE
    assert calls == []


def test_chunked_body_at_the_limit_is_handled_normally():
    client, _ = _build()

    def chunks():
        yield b"x" * 60
        yield b"x" * 40

    assert client.post("/echo", content=chunks()).json() == {"size": LIMIT}


def test_request_without_a_body_is_unaffected():
    client, _ = _build()

    assert client.get("/ping").json() == {"ok": True}


def test_unrelated_handler_errors_are_not_swallowed():
    client, _ = _build()

    assert client.post("/boom", content=b"x").status_code == 500


def test_lifespan_scope_passes_through():
    client, _ = _build()

    with client:  # runs the lifespan protocol through the middleware
        assert client.get("/ping").status_code == 200


def test_response_already_started_before_the_limit_is_passed_is_left_alone():
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        while (await receive())["type"] == "http.request":
            pass  # keep reading the body after having started to answer
        await send({"type": "http.response.body", "body": b"late"})

    def chunks():
        for _ in range(5):
            yield b"x" * 30

    client = TestClient(BodySizeLimitMiddleware(app, limit=LIMIT))
    response = client.post("/", content=chunks())

    # Too late for a 413: the status line is out and cannot be taken back.
    assert response.status_code == 200


def test_app_keeps_getting_disconnect_after_rejection_and_client_disconnect_passes():
    import asyncio

    seen = []
    sent = []

    async def app(scope, receive, send):
        while True:
            message = await receive()
            seen.append(message["type"])
            if len(seen) >= 4:
                return

    feed = iter([
        {"type": "http.request", "body": b"x" * 150, "more_body": True},
        {"type": "http.disconnect"},
    ])

    async def receive():
        return next(feed)

    async def send(message):
        sent.append(message)

    scope = {"type": "http", "headers": []}
    asyncio.run(BodySizeLimitMiddleware(app, limit=LIMIT)(scope, receive, send))

    assert seen == ["http.disconnect"] * 4
    assert sent[0]["status"] == 413

    # Below the limit a client disconnect is the app's to see, unchanged.
    seen.clear()
    feed = iter([{"type": "http.disconnect"}] * 4)
    asyncio.run(BodySizeLimitMiddleware(app, limit=LIMIT)(scope, receive, send))
    assert seen == ["http.disconnect"] * 4
