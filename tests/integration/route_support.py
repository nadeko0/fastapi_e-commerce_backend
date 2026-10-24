"""A probe route on the shared application, removed again.

Several tests add a route to the one global `app` to reach a code path no real
route reaches (an unhandled exception, a header on a private path, a document
built with an extra operation). The route has to go when the test ends, and so
does the OpenAPI document built while it existed: FastAPI caches the document
and rebuilds it only when a route is *added* (the router's routes version), so
removing the route alone leaves the probe in `app.openapi_schema` for every
later test in the process (tests/unit/test_openapi_snapshot.py failed that way
after test_openapi_responses.py).
"""
from contextlib import contextmanager

from app.main import app


@contextmanager
def probe_routes():
    """Restore the router's routes and drop the cached OpenAPI document on exit,
    however many routes the body added."""
    saved = list(app.router.routes)
    try:
        yield app
    finally:
        app.router.routes[:] = saved
        app.openapi_schema = None
