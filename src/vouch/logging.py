"""Central logging for Vouch.

Usage:
    from vouch.logging import get_logger

    logger = get_logger("database")
    logger.info("submission %s persisted", submission_id)
"""

import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

CLI_FORMAT = "[%(name)s]: %(message)s"
FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s"

LOG_FILE = Path("logs") / "vouch.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

LEVEL_ENV_VAR = "VOUCH_LOG_LEVEL"
DEFAULT_LEVEL = "INFO"

_lock = threading.Lock()
_configured = False


def _configure() -> None:
    """Attach the CLI and file handlers to the root logger exactly once.

    Called lazily on first get_logger() so importing any module is enough to
    get working logging, and repeated calls never stack duplicate handlers.
    """
    global _configured
    if _configured:
        return

    with _lock:
        if _configured:
            return

        level = os.environ.get(LEVEL_ENV_VAR, DEFAULT_LEVEL).upper()

        root = logging.getLogger()
        root.setLevel(level)

        cli_handler = logging.StreamHandler()
        cli_handler.setFormatter(logging.Formatter(CLI_FORMAT))

        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            LOG_FILE,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
        )
        file_handler.setFormatter(logging.Formatter(FILE_FORMAT))

        root.addHandler(cli_handler)
        root.addHandler(file_handler)

        _configured = True


def get_logger(component: str) -> logging.Logger:
    """Return a logger whose name is the component that logs with it."""
    _configure()
    return logging.getLogger(component)
