"""The legal routes run on the async session: no sync DB access on the loop.

Focused stand-in for the project-wide static guard: walk the legal router's
routes and check that no dependency is a sync session dependency and that no
handler uses the sync Query API.
"""

import inspect

from fastapi.routing import APIRoute

import app.core.database as database
from app.api.v1 import legal

# Any dependency defined in app.core.database other than get_async_db.
LEGAL_DB_ROUTES = {
    "update_user_consent",
    "request_personal_data",
}


def _legal_routes():
    # The router's own routes (the app may mount it under a prefix).
    return [r for r in legal.router.routes if isinstance(r, APIRoute)]


def _calls(dependant):
    for sub in dependant.dependencies:
        yield sub.call
        yield from _calls(sub)


def test_no_legal_route_depends_on_a_sync_session():
    routes = _legal_routes()
    assert len(routes) == 7
    for route in routes:
        for call in _calls(route.dependant):
            if call.__module__ == database.__name__:
                assert call is database.get_async_db, route.path


def test_legal_db_routes_share_the_auth_async_session():
    by_name = {r.endpoint.__name__: r for r in _legal_routes()}
    for name in LEGAL_DB_ROUTES:
        assert database.get_async_db in set(_calls(by_name[name].dependant)), name


def test_routes_that_never_touch_the_database_declare_no_db_parameter():
    # (get_async_db still resolves for them through the auth dependency.)
    by_name = {r.endpoint.__name__: r for r in _legal_routes()}
    for name in ("get_consent_status", "check_data_retention"):
        assert "db" not in inspect.signature(by_name[name].endpoint).parameters, name


def test_legal_handlers_are_coroutines_without_sync_query_api():
    for route in _legal_routes():
        if route.endpoint.__name__ in {
            "update_user_consent", "request_personal_data",
            "get_consent_status", "check_data_retention",
        }:
            assert inspect.iscoroutinefunction(route.endpoint), route.path
    source = inspect.getsource(legal)
    assert ".query(" not in source
