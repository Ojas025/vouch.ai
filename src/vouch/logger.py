"""Central logging for Vouch.

Usage:
    from vouch.logger import configure_logging, get_logger, shutdown_logging

    configure_logging()                  # once, at process start
    logger = get_logger(__name__)
    logger.info("submission %s persisted", submission_id)

Records reach a ``QueueHandler`` that feeds a background ``QueueListener``,
which fans out to a console handler and a rotating JSON file handler. Emitting a
record therefore never performs blocking file I/O on the calling thread, which
matters because the webhook handlers and outbox relay run on the event loop.

Configuration is declarative (``dictConfig``) so the topology is inspectable in
one place and re-applying it cannot stack duplicate handlers. Structured output
comes from ``_JsonFormatter`` rather than a third-party dependency.
"""

import copy
import json
import logging
import logging.config
import logging.handlers
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from vouch.observability.records import install_record_factory, restore_record_factory
from vouch.observability.sanitize import sanitize, scrub_text

LEVEL_ENV_VAR = "VOUCH_LOG_LEVEL"
LOG_DIR_ENV_VAR = "VOUCH_LOG_DIR"
DEFAULT_LEVEL = "INFO"
DEFAULT_LOG_DIR = Path("logs")
LOG_FILE_NAME = "vouch.log"

CONSOLE_FORMAT = "[%(name)s]: %(message)s"

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

#: Root carries only this handler; it doubles as the marker that detects
#: whether logging is already configured.
QUEUE_HANDLER_NAME = "vouch_queue"
LISTENER_ATTR = "_vouch_listener"

#: Bumped whenever the emitted field set changes shape, so a log consumer can
#: branch on it instead of guessing from the keys it happens to find.
SCHEMA_VERSION = 1

#: Security-relevant records travel on their own logger so they can be routed,
#: retained and levelled independently of operational noise.
AUDIT_LOGGER_NAME = "vouch.audit"

#: Independent of VOUCH_LOG_LEVEL: an audit trail that vanishes because an
#: operator raised the application log level is not an audit trail.
AUDIT_LOGGER_LEVEL = "INFO"

_lock = threading.Lock()


class _PreservingQueueHandler(logging.handlers.QueueHandler):
    """Queue handler that hands the record downstream intact.

    The stock ``prepare`` runs the record through this handler's own formatter,
    which replaces ``msg`` with the already-rendered console line, and then
    nulls ``exc_info`` — so downstream handlers see a double-prefixed message
    and no traceback at all. Rendering ``getMessage()`` instead keeps the
    message clean and the traceback available to the JSON formatter.

    ``record.getMessage()`` is used rather than ``Formatter.format`` because
    ``format`` assigns ``record.exc_text`` as a side effect, and the copy that
    travels through the queue would then carry a stale traceback that the
    downstream handler appends a second time.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        message = record.getMessage()
        prepared = copy.copy(record)
        prepared.msg = message
        prepared.args = None
        return prepared


class _JsonFormatter(logging.Formatter):
    """Render records as single-line JSON for log aggregation.

    Fields already on the record are never dropped, so the ``event`` /
    ``component`` / ``reason_code`` / ``error_category`` extras that call sites
    have always passed through ``extra=`` keep flowing. Sanitisation and the
    UTC timestamp are applied here rather than at the call site, so every
    record crossing this formatter gets the same treatment.
    """

    _RESERVED = frozenset(
        logging.LogRecord("", 0, "", 0, "", (), None).__dict__
    ) | {"asctime", "message", "taskName"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(
                timespec="microseconds"
            ).replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "lineno": record.lineno,
            "message": sanitize(record.getMessage()),
        }

        # Anything the caller attached via ``extra=``, plus what the record
        # factory layered on (trace and correlation context).
        for key, value in record.__dict__.items():
            if key not in self._RESERVED and not key.startswith("_"):
                payload[key] = sanitize(value)

        if record.exc_info:
            payload["exception"] = scrub_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack"] = scrub_text(self.formatStack(record.stack_info))

        return json.dumps(payload, default=str)


def _validate_level(level: str | int | None) -> int:
    """Coerce ``level`` to a logging level integer, falling back to INFO.

    Level comes from the environment, so a typo in ``VOUCH_LOG_LEVEL`` must not
    take down the process that is trying to report the typo.
    """
    if level is None:
        level = os.environ.get(LEVEL_ENV_VAR, DEFAULT_LEVEL)

    if isinstance(level, int):
        return level

    return logging.getLevelNamesMapping().get(str(level).strip().upper(), logging.INFO)


def _build_config(level: int, log_dir: Path, log_queue: queue.SimpleQueue) -> dict[str, Any]:
    """Return the dictConfig document describing the logging topology.

    ``log_queue`` is passed in because the listener must drain the very queue
    the handler writes to; dictConfig cannot express that shared reference.
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "console": {"format": CONSOLE_FORMAT},
            "json": {"()": _JsonFormatter},
        },
        "handlers": {
            QUEUE_HANDLER_NAME: {
                "()": _PreservingQueueHandler,
                # NOTSET, and this must stay paired with the vouch.audit entry
                # below. A logger's own effective level is checked before any
                # handler sees the record, so VOUCH_LOG_LEVEL cannot suppress
                # vouch.audit's INFO records — but this handler's level would.
                # Both halves are required or the audit trail silently vanishes.
                "level": "NOTSET",
                "queue": log_queue,
            },
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "console",
                "level": "INFO",
            },
            "file": {
                "class": "logging.handlers.RotatingFileHandler",
                "formatter": "json",
                "filename": str(log_dir / LOG_FILE_NAME),
                "maxBytes": MAX_BYTES,
                "backupCount": BACKUP_COUNT,
                # Keep the file unopened until the first record, so merely
                # configuring logging does not create it.
                "delay": True,
                "encoding": "utf-8",
            },
        },
        # The real handlers are listed so dictConfig attaches them, which is how
        # configure_logging() recovers them to hand to the listener. It detaches
        # them again straight after, leaving only the queue handler on root.
        "root": {"level": level, "handlers": [QUEUE_HANDLER_NAME, "console", "file"]},
        "loggers": {
            # Uvicorn must keep propagating so its records reach our handlers.
            "uvicorn": {"level": "INFO", "propagate": True},
            "uvicorn.error": {"level": "INFO", "propagate": True},
            "uvicorn.access": {"level": "INFO", "propagate": True},
            # Explicit level defeats inheritance from root, which is what lets
            # the queue handler's NOTSET deliver audit records.
            AUDIT_LOGGER_NAME: {"level": AUDIT_LOGGER_LEVEL, "propagate": True},
        },
    }


def _is_configured() -> bool:
    """True when our queue handler is currently attached to the root logger.

    Checks the root logger's live handlers rather than ``getHandlerNames()``,
    which is a global registry of every handler ever constructed and so would
    report a detached handler as configured.
    """
    return any(
        handler.name == QUEUE_HANDLER_NAME
        for handler in logging.getLogger().handlers
    )


def configure_logging(
    level: str | int | None = None,
    log_dir: str | Path | None = None,
) -> None:
    """Install the logging topology. Safe to call more than once.

    A second call is a no-op: the installed ``vouch_queue`` handler is detected,
    so repeated calls cannot stack duplicate handlers. Use ``shutdown_logging``
    to tear the topology down again.
    """
    with _lock:
        if _is_configured():
            return

        # Installed before dictConfig so records emitted during configuration
        # already carry their trace context.
        install_record_factory()

        if log_dir is None:
            log_dir = Path(os.environ.get(LOG_DIR_ENV_VAR, DEFAULT_LOG_DIR))
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)

        log_queue: queue.SimpleQueue = queue.SimpleQueue()
        logging.config.dictConfig(
            _build_config(_validate_level(level), log_dir, log_queue)
        )

        root = logging.getLogger()
        targets = [h for h in root.handlers if h.name != QUEUE_HANDLER_NAME]
        for handler in targets:
            root.removeHandler(handler)

        queue_handler = next(h for h in root.handlers if h.name == QUEUE_HANDLER_NAME)
        listener = logging.handlers.QueueListener(
            log_queue, *targets, respect_handler_level=True
        )
        listener.start()
        setattr(queue_handler, LISTENER_ATTR, listener)


def shutdown_logging() -> None:
    """Stop the listener thread and detach the queue handler.

    ``QueueListener.stop`` drains the queue before returning, so records already
    emitted are flushed rather than discarded. Safe to call when logging was
    never configured, which is the case for most tests.
    """
    root = logging.getLogger()

    for handler in root.handlers[:]:
        if handler.name != QUEUE_HANDLER_NAME:
            continue
        listener = getattr(handler, LISTENER_ATTR, None)
        root.removeHandler(handler)
        if listener is not None:
            listener.stop()
            setattr(handler, LISTENER_ATTR, None)

    # Only reverts a factory this module installed, so a caller that never
    # configured logging leaves whatever the process already had in place.
    restore_record_factory()


def get_audit_logger() -> logging.Logger:
    """Logger for security audit events. Same pipeline as operational logs."""
    return logging.getLogger(AUDIT_LOGGER_NAME)


def get_logger(name: str) -> logging.Logger:
    """Return the logger for ``name``.

    Callers pass ``__name__`` so records land in the standard dotted hierarchy
    and per-module levels remain tunable.
    """
    return logging.getLogger(name)
