"""Celery's own task and schedule lines are written at the worker's
--loglevel, through the application's JSON logging (app/tasks.py,
_configure_logging on Celery's setup_logging signal)."""

import logging

import pytest
from celery.signals import setup_logging as celery_setup_logging
from celery.utils.log import get_logger as celery_get_logger

import app.tasks  # noqa: F401  (connects the signal receiver)
from app.core.config import settings
from app.core.logging_config import JSONFormatter

# "Task ... received" (worker), "Task ... succeeded/failed" (the pool's
# tracer), "Scheduler: Sending due task" (beat).
CELERY_LOGGERS = ("celery.worker.strategy", "celery.app.trace", "celery.beat")


@pytest.fixture
def production_logging(monkeypatch):
    """The logging state a deployed worker gets, restored afterwards."""
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    monkeypatch.setattr(settings, "LOG_FILE", "")
    root = logging.getLogger()
    celery_logger = logging.getLogger("celery")
    app_logger = logging.getLogger("app")
    saved = (root.handlers[:], root.level, celery_logger.level, app_logger.level)
    # A fresh worker process has no level on "celery"; an earlier test in the
    # same process may have set one.
    celery_logger.setLevel(logging.NOTSET)
    yield
    for handler in root.handlers:
        if handler not in saved[0]:
            handler.close()
    root.handlers, root.level = saved[0], saved[1]
    celery_logger.setLevel(saved[2])
    app_logger.setLevel(saved[3])


def test_celery_lines_are_written_at_the_workers_loglevel(production_logging):
    celery_setup_logging.send(sender=None, loglevel=logging.INFO, logfile=None, format="", colorize=False)

    for name in CELERY_LOGGERS:
        assert celery_get_logger(name).isEnabledFor(logging.INFO), name
    # Everything else third-party stays at the production root level.
    assert not logging.getLogger("sqlalchemy.engine").isEnabledFor(logging.INFO)
    # And the lines go out as the application's JSON.
    assert any(isinstance(handler.formatter, JSONFormatter) for handler in logging.getLogger().handlers)


def test_without_a_loglevel_celery_keeps_the_root_level(production_logging):
    celery_setup_logging.send(sender=None)

    assert not celery_get_logger("celery.beat").isEnabledFor(logging.INFO)
    assert celery_get_logger("celery.beat").isEnabledFor(logging.WARNING)
