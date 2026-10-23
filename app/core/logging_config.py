import json
import logging
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import settings
from app.core.request_context import get_request_context

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

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        # RFC 3339 section 5.6 date-time in UTC, e.g. 2026-10-23T14:05:09.123Z.
        # strftime's %z gives "+0000" (no colon), which RFC 3339 does not allow.
        moment = datetime.fromtimestamp(record.created, tz=UTC)
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        context = get_request_context()
        if context is not None:
            payload["request_id"] = context.request_id
            if context.trace_id:
                payload["trace_id"] = context.trace_id
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


def setup_logging(config=settings):
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
    # Third-party loggers (sqlalchemy.engine, uvicorn.access, httpx...) inherit
    # the root level, so keep it quiet outside development. The application's
    # own loggers ("app.*") get LOG_LEVEL, otherwise GDPR, security and
    # payment events logged at INFO would never be written in production.
    root_logger.setLevel(logging.INFO if config.ENVIRONMENT == "development" else logging.WARNING)
    logging.getLogger("app").setLevel(config.LOG_LEVEL)

    root_logger.handlers = []
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    startup_logger = logging.getLogger(__name__)
    startup_logger.info(
        f"Starting application in {config.ENVIRONMENT} environment "
        f"(Version: {config.VERSION})"
    )
    startup_logger.info(f"Log directory: {log_dir.absolute()}")
    startup_logger.info("Logging system initialized with file rotation (100MB max, 5 backups)")
