"""app/api/deps.get_db, the session dependency of the legal router. The
suite overrides it (tests/conftest.py), so the real one is exercised
here directly."""

import pytest

import app.api.deps as deps


class _Session:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_get_db_yields_a_session_and_closes_it(monkeypatch):
    session = _Session()
    monkeypatch.setattr(deps, "SessionLocal", lambda: session)

    dependency = deps.get_db()
    assert next(dependency) is session
    with pytest.raises(StopIteration):
        next(dependency)

    assert session.closed is True


def test_get_db_closes_the_session_when_the_route_fails(monkeypatch):
    session = _Session()
    monkeypatch.setattr(deps, "SessionLocal", lambda: session)

    dependency = deps.get_db()
    next(dependency)
    with pytest.raises(RuntimeError):
        dependency.throw(RuntimeError("route failed"))

    assert session.closed is True
