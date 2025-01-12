import os
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import settings

log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

def setup_logging():
    formatter = logging.Formatter(
        fmt='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )


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

setup_logging()