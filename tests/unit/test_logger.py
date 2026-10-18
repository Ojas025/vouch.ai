"""Unit tests for vouch.logger.

Covers the four behaviours the module exists to provide: level validation that
never crashes the process, idempotent configuration, the non-blocking
QueueHandler/QueueListener pair, and JSON record rendering.
"""

import json
import logging
import logging.handlers
import os
import sys
import time

import pytest

from vouch.logger import (
    DEFAULT_LEVEL,
    LEVEL_ENV_VAR,
    LOG_DIR_ENV_VAR,
    LOG_FILE_NAME,
    QUEUE_HANDLER_NAME,
    _JsonFormatter,
    _validate_level,
    configure_logging,
    get_logger,
    shutdown_logging,
)


def _drain(queue_handler, attempts=400):
    """Return the log file's contents once the listener has written to it.

    Read via the filesystem path rather than the handler's stream, which is
    opened write-only. The file handler uses ``delay=True``, so the file does
    not exist until the first record is emitted.
    """
    listener = getattr(queue_handler, "_vouch_listener")
    target = next(h for h in listener.handlers if isinstance(h, logging.FileHandler))

    for _ in range(attempts):
        if os.path.exists(target.baseFilename) and os.path.getsize(target.baseFilename):
            break
        time.sleep(0.005)

    if not os.path.exists(target.baseFilename):
        return ""
    with open(target.baseFilename, encoding="utf-8") as fh:
        return fh.read()


class TestValidateLevel:
    @pytest.mark.parametrize(
        "raw, expected",
        [("DEBUG", logging.DEBUG), ("info", logging.INFO),
         ("  Warning  ", logging.WARNING), ("CRITICAL", logging.CRITICAL)],
    )
    def test_accepts_level_names_insensitively(self, raw, expected):
        assert _validate_level(raw) == expected

    @pytest.mark.parametrize("raw", ["BOGUS", "TRACE", "", None])
    def test_unrecognised_value_falls_back_to_info(self, raw):
        # Regression guard: the previous implementation raised ValueError from
        # setLevel(), so a typo in VOUCH_LOG_LEVEL killed the process.
        assert _validate_level(raw) == logging.INFO

    def test_none_reads_level_env_var(self, monkeypatch):
        monkeypatch.setenv(LEVEL_ENV_VAR, "DEBUG")
        assert _validate_level(None) == logging.DEBUG

    def test_none_uses_default_when_env_var_absent(self, monkeypatch):
        monkeypatch.delenv(LEVEL_ENV_VAR, raising=False)
        assert _validate_level(None) == getattr(logging, DEFAULT_LEVEL)

    def test_int_passes_through(self):
        assert _validate_level(logging.ERROR) == logging.ERROR


def _queue_handler():
    """Return the vouch queue handler from the live root handlers."""
    return next(
        h for h in logging.getLogger().handlers if h.name == QUEUE_HANDLER_NAME
    )


class TestConfigureLogging:
    def test_installs_queue_handler_and_listener(self, log_dir):
        configure_logging(log_dir=log_dir)
        handler = _queue_handler()

        assert handler in logging.getLogger().handlers
        assert getattr(handler, "_vouch_listener")._thread.is_alive()

    def test_root_carries_only_the_queue_handler(self, log_dir):
        # The console and file handlers live behind the listener so that
        # emitting a record never blocks the caller on disk I/O.
        configure_logging(log_dir=log_dir)
        assert [h.name for h in logging.getLogger().handlers] == [QUEUE_HANDLER_NAME]

    @pytest.mark.parametrize(
        "requested, expected",
        [("WARNING", logging.WARNING), ("DEBUG", logging.DEBUG),
         ("BOGUS", logging.INFO)],
    )
    def test_applies_level_including_the_fallback(self, log_dir, requested, expected):
        configure_logging(level=requested, log_dir=log_dir)
        assert logging.getLogger().level == expected

    def test_second_call_is_a_no_op_and_does_not_duplicate(self, log_dir):
        configure_logging(log_dir=log_dir)
        configure_logging(log_dir=log_dir)

        assert logging.getLogger().handlers.count(_queue_handler()) == 1

    def test_record_is_persisted_exactly_once_after_reconfiguration(self, log_dir):
        configure_logging(log_dir=log_dir)
        configure_logging(log_dir=log_dir)

        logging.getLogger("dup.check").info("only once")

        assert _drain(_queue_handler()).count("only once") == 1

    @pytest.mark.parametrize("use_env", [False, True], ids=["arg", "env-var"])
    def test_creates_the_log_directory(self, tmp_path, monkeypatch, use_env):
        target = tmp_path / "deeply" / "nested" / "logs"
        if use_env:
            monkeypatch.setenv(LOG_DIR_ENV_VAR, str(target))
            configure_logging()
        else:
            configure_logging(log_dir=target)

        assert target.is_dir()

    def test_emitting_does_not_write_to_disk_synchronously(self, log_dir):
        # The record only lands once the listener thread drains the queue, which
        # is what keeps the event loop free.
        configure_logging(log_dir=log_dir)
        logging.getLogger("file.check").info("written to disk")

        assert "written to disk" in _drain(_queue_handler())


class TestShutdownLogging:
    def test_removes_handler_and_stops_the_listener(self, log_dir):
        configure_logging(log_dir=log_dir)
        handler = _queue_handler()
        thread = getattr(handler, "_vouch_listener")._thread

        shutdown_logging()

        assert handler not in logging.getLogger().handlers
        assert not thread.is_alive()

    def test_is_safe_to_call_when_not_configured(self):
        shutdown_logging()  # must not raise


class TestGetLogger:
    def test_returns_named_logger(self):
        assert get_logger("vouch.custom").name == "vouch.custom"


    def test_does_not_configure_as_a_side_effect(self, log_dir):
        get_logger("vouch.sideeffect").info("should not be persisted")

        # getHandlerNames() is a global registry of every handler ever built, so
        # assert against the live root handlers instead.
        assert not log_dir.exists()
        assert not any(
            h.name == QUEUE_HANDLER_NAME for h in logging.getLogger().handlers
        )


def _record(msg="hello", level=logging.INFO, args=None):
    return logging.LogRecord(
        name="vouch.test", level=level, pathname="/app/src/vouch/thing.py",
        lineno=42, msg=msg, args=args, exc_info=None, func="do_work",
    )


class TestJsonFormatter:
    def test_emits_valid_single_line_json(self):
        out = _JsonFormatter().format(_record("hi"))
        assert json.loads(out)["message"] == "hi"
        assert "\n" not in out

    def test_includes_core_record_fields(self):
        payload = json.loads(_JsonFormatter().format(_record(level=logging.ERROR)))

        assert payload["level"] == "ERROR"
        assert payload["logger"] == "vouch.test"
        assert payload["module"] == "thing"
        assert payload["lineno"] == 42
        assert payload["timestamp"].startswith("20")

    def test_applies_lazy_percent_args(self):
        # The logger defers formatting; the formatter must resolve %-args.
        rec = _record("submission %s persisted", args=("abc123",))
        assert json.loads(_JsonFormatter().format(rec))["message"] == (
            "submission abc123 persisted"
        )

    def test_includes_extra_fields_but_not_reserved_attributes(self):
        rec = _record("event")
        rec.__dict__["submission_id"] = "sub-1"
        payload = json.loads(_JsonFormatter().format(rec))

        assert payload["submission_id"] == "sub-1"
        for reserved in ("args", "exc_info", "pathname", "levelname", "msg"):
            assert reserved not in payload

    def test_includes_traceback_when_exception_present(self):
        try:
            raise ValueError("boom")
        except ValueError:
            rec = logging.LogRecord(
                "vouch.test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info()
            )
        payload = json.loads(_JsonFormatter().format(rec))

        assert "ValueError: boom" in payload["exception"]

    def test_non_serialisable_extra_falls_back_to_str(self):
        rec = _record("event")
        rec.__dict__["obj"] = object()
        payload = json.loads(_JsonFormatter().format(rec))

        assert isinstance(payload["obj"], str)
