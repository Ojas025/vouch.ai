"""Shared fixtures for the Vouch test suite.

Two environment concerns are handled here before any application import
happens:

1. ``vouch.db.session`` and ``vouch.api.server`` read ``os.environ[...]`` at
   module import time and raise ``KeyError`` when a variable is absent, so
   every required variable must be present before those modules load.
2. Importing through two different roots (``vouch.*`` and ``src.vouch.*``)
   registers the SQLAlchemy models twice against two different ``Base``
   objects, which raises ``InvalidRequestError: Table 'submissions' is already
   defined``. Ensuring the canonical ``vouch`` root is the only importable one
   keeps a single registry.

Logging is redirected into tmp_path and reset around every test:
``configure_logging`` mutates global state and starts a listener thread, so
without this its handlers leak between tests and ``logs/`` is written into the
repository working tree.
"""

import logging
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Spelled literally rather than imported from vouch.logger so that this
# conftest still loads against a tree where logger.py has not been rewritten.
LOG_DIR_ENV_VAR = "VOUCH_LOG_DIR"

# Required by vouch.db.session (POSTGRES_URL) and vouch.api.server
# (WHATSAPP_SECRET, TELEGRAM_WEBHOOK_SECRET, REDIS_URL), all read at import.
REQUIRED_ENV = {
    "POSTGRES_URL": "postgresql+psycopg://user:pass@localhost:5432/vouch_test",
    "REDIS_URL": "redis://localhost:6379/15",
    "WHATSAPP_SECRET": "test-whatsapp-secret",
    "WHATSAPP_ACCESS_TOKEN": "test-whatsapp-access-token",
    "TELEGRAM_WEBHOOK_SECRET": "test-telegram-secret",
}

# Remove the repo root from sys.path so `src.vouch...` cannot be imported and
# the duplicate-Base problem cannot occur.
_SRC_ROOT = str(REPO_ROOT)
sys.path[:] = [p for p in sys.path if p not in (_SRC_ROOT, str(REPO_ROOT / "src"))]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

for _key, _value in REQUIRED_ENV.items():
    os.environ.setdefault(_key, _value)


@pytest.fixture(autouse=True)
def isolate_log_dir(tmp_path, monkeypatch):
    """Point every test's logging at a temporary directory.

    Without this, importing the app (which calls ``configure_logging()`` from
    its lifespan) creates ``logs/vouch.log`` in the repository working tree,
    because ``DEFAULT_LOG_DIR`` is relative to the current directory.
    """
    monkeypatch.setenv(LOG_DIR_ENV_VAR, str(tmp_path / "logs"))


@pytest.fixture(autouse=True)
def reset_logging():
    """Restore global logging state after each test.

    ``configure_logging`` installs handlers on the root logger and starts a
    listener thread. Without this fixture those handlers accumulate across
    tests (duplicating records) and the listener threads leak.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level

    yield

    from vouch.logger import shutdown_logging

    shutdown_logging()
    for handler in root.handlers[:]:
        if handler not in saved_handlers:
            root.removeHandler(handler)
    for handler in saved_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(saved_level)


@pytest.fixture
def log_dir(tmp_path):
    """The log directory this test is isolated into."""
    return tmp_path / "logs"
