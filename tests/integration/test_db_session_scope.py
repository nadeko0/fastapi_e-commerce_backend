"""get_async_db finishes before the response is sent.

A yield dependency has the "request" scope by default: its code after the
yield runs after the response went out. For get_async_db that code is the
final commit and the SQLAlchemyError -> 500 mapping, so a commit that failed
was invisible to a client that already had its 200, and the pool connection
stayed checked out while the response was written. Declared with
scope="function" it ends right after the path operation function returns.

Every use must carry the scope: the dependency cache is keyed by it, so one
route mixing scopes would get two sessions, and a request-scoped generator
cannot depend on a function-scoped one at all.
"""

from typing import Annotated, Any

from fastapi import Depends, FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

import app.core.database as database_module
from app.main import app


class _RecordingSession:
    def __init__(self, events, fail_commit=False):
        self.events = events
        self.fail_commit = fail_commit

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def commit(self):
        self.events.append("commit")
        if self.fail_commit:
            raise OperationalError("COMMIT", {}, Exception("deferred constraint"))

    async def rollback(self):
        self.events.append("rollback")

    async def close(self):
        self.events.append("close")


class _RecordingResponse(JSONResponse):
    events: list

    async def __call__(self, scope, receive, send):
        self.events.append("send")
        await super().__call__(scope, receive, send)


def _app(monkeypatch, events, fail_commit=False):
    monkeypatch.setattr(
        database_module,
        "AsyncSessionLocal",
        lambda: _RecordingSession(events, fail_commit),
    )
    response_class = type("R", (_RecordingResponse,), {"events": events})
    throwaway = FastAPI()

    @throwaway.get("/write", response_class=response_class)
    async def write(db: Annotated[Any, Depends(database_module.get_async_db, scope="function")]):
        return {"ok": 1}

    return throwaway


def test_failing_final_commit_answers_500_not_200(monkeypatch):
    events = []
    with TestClient(_app(monkeypatch, events, fail_commit=True),
                    raise_server_exceptions=False) as c:
        response = c.get("/write")
    assert response.status_code == 500
    assert response.json() == {"detail": "A database error occurred"}
    assert events == ["commit", "rollback", "close"]


def test_session_is_committed_and_closed_before_the_response_is_sent(monkeypatch):
    events = []
    with TestClient(_app(monkeypatch, events)) as c:
        response = c.get("/write")
    assert response.status_code == 200
    assert events == ["commit", "close", "send"]


def test_every_use_of_get_async_db_is_function_scoped():
    def walk(dependant):
        for sub in dependant.dependencies:
            yield sub
            yield from walk(sub)

    def api_routes(routes):
        # include_router() wraps the original router in the current FastAPI.
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            elif hasattr(route, "original_router"):
                yield from api_routes(route.original_router.routes)

    seen = 0
    for route in api_routes(app.routes):
        for sub in walk(route.dependant):
            if sub.call is database_module.get_async_db:
                seen += 1
                assert sub.scope == "function", route.path
    assert seen > 50
