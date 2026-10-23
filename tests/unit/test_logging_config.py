"""Production logging: application INFO events are written, timestamps are RFC 3339."""

import json
import logging
import re

import pytest

from app.core import logging_config
from app.core.config import Settings, settings

RFC_3339_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@pytest.fixture
def configure(tmp_path, monkeypatch):
    root = logging.getLogger()
    app_logger = logging.getLogger("app")
    saved = (root.handlers[:], root.level, app_logger.level)
    monkeypatch.setattr(logging_config, "log_dir", tmp_path)

    def run(**values):
        logging_config.setup_logging(settings.model_copy(update=values))
        for handler in root.handlers:
            handler.flush()
        return tmp_path / "app.log"

    yield run
    for handler in root.handlers:
        handler.close()
    root.handlers, root.level = saved[0], saved[1]
    app_logger.setLevel(saved[2])


def _records(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("name", ["app.api.v1.users", "app.core.security", "app.services.redis"])
def test_production_writes_info_from_the_application_loggers(configure, name):
    path = configure(ENVIRONMENT="production")
    logging.getLogger(name).info("gdpr export queued for user 7")
    messages = [r["message"] for r in _records(path)]
    assert "gdpr export queued for user 7" in messages


@pytest.mark.parametrize("name", ["sqlalchemy.engine", "uvicorn.access", "httpx", "httpcore"])
def test_production_still_drops_info_from_third_party_loggers(configure, name):
    path = configure(ENVIRONMENT="production")
    logging.getLogger(name).info("noisy third-party line")
    assert "noisy third-party line" not in path.read_text(encoding="utf-8")


def test_production_does_not_write_debug(configure):
    path = configure(ENVIRONMENT="production")
    logging.getLogger("app.api.v1.users").debug("debug line")
    assert "debug line" not in path.read_text(encoding="utf-8")


def test_log_level_raises_the_threshold(configure):
    path = configure(ENVIRONMENT="production", LOG_LEVEL="WARNING")
    logging.getLogger("app.api.v1.users").info("info line")
    logging.getLogger("app.api.v1.users").warning("warning line")
    messages = [r["message"] for r in _records(path)]
    assert "warning line" in messages
    assert "info line" not in messages


def test_log_level_defaults_to_info():
    assert Settings(_env_file=None).LOG_LEVEL == "INFO"


def test_debug_is_refused_outside_development_and_test():
    with pytest.raises(ValueError, match="LOG_LEVEL"):
        Settings(_env_file=None, ENVIRONMENT="production", LOG_LEVEL="DEBUG")
    assert Settings(_env_file=None, ENVIRONMENT="development", LOG_LEVEL="DEBUG")


def test_timestamps_are_rfc_3339_utc_with_milliseconds(configure):
    path = configure(ENVIRONMENT="production")
    logging.getLogger("app.x").info("stamped")
    stamps = [r["timestamp"] for r in _records(path)]
    assert stamps
    assert all(RFC_3339_UTC.match(s) for s in stamps), stamps


def test_timestamp_is_the_record_creation_time_in_utc():
    record = logging.LogRecord("app.x", logging.INFO, "f", 1, "m", (), None)
    record.created = 1792764309.123  # 2026-10-23T14:05:09.123Z
    assert logging_config.JSONFormatter().formatTime(record) == "2026-10-23T14:05:09.123Z"
