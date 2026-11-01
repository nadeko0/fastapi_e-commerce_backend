"""Shared setup for the runtime torture & stateful fuzzing suite.

Import this module first in every torture test module:

    import torture_support  # noqa: F401  (must precede payment_support & co.)

The project-wide fixtures (client, db_session, fake_redis, async_session_factory,
the per-test table cleanup and the optional PostgreSQL database selected by
TEST_DATABASE_URL) come from tests/conftest.py, which pytest applies here too.
There is deliberately no conftest.py in this directory: tests/integration's
helpers do `from conftest import TestingSessionLocal`, and a second module named
conftest would shadow the project one.

This module makes the integration helpers (payment_support, stripe_event_support,
time_support, ...) importable from this directory and exposes `requires_postgres`.
"""
import os
import sys

import pytest

_INTEGRATION = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "integration")
if _INTEGRATION not in sys.path:
    sys.path.insert(0, _INTEGRATION)

from conftest import IS_POSTGRES  # noqa: E402  (tests/conftest.py)

# Concurrency/lock tests are only meaningful on a real server (SQLite serialises
# every writer). They are skipped on the default SQLite run with this reason.
requires_postgres = pytest.mark.skipif(
    not IS_POSTGRES,
    reason="needs a real PostgreSQL server: set TEST_DATABASE_URL=postgresql+psycopg://...",
)
