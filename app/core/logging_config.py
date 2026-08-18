import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import settings

log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

# Standard LogRecord attributes, used to detect caller-supplied `extra={...}`
# fields so they can be folded into the JSON output rather than dropped.
_RESERVED_RECORD_ATTRS = frozenset(logging.LogRecord(
    "", 0, "", 0, "", (), None
).__dict__.keys()) | {"message", "asctime"}


class JSONFormatter(logging.Formatter):
    """Structured (one-line JSON per record) log formatter.

    Plain-text logs are hard to query/filter in any log aggregator; JSON
    lines are the standard shape most log pipelines expect out of the box.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        extra = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _RESERVED_RECORD_ATTRS
        }
        if extra:
            payload["extra"] = extra

        return json.dumps(payload, default=str)


def setup_logging():
    formatter = JSONFormatter()

    file_handler = RotatingFileHandler(
        filename=log_dir / "app.log",
        maxBytes=100 * 1024 * 1024,
        backupCount=5,
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.INFO)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.WARNING)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO if settings.ENVIRONMENT == "development" else logging.WARNING)

    root_logger.handlers = []
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    root_logger.info(
        f"Starting application in {settings.ENVIRONMENT} environment "
        f"(Version: {settings.VERSION})"
    )
    root_logger.info(f"Log directory: {log_dir.absolute()}")
    root_logger.info("Logging system initialized with file rotation (100MB max, 5 backups)")
