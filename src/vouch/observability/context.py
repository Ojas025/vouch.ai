"""Per-task correlation identifiers for logs, audit events and spans.

ContextVars rather than a thread-local because the interesting boundaries are
asynchronous: a fan-out branch and its parent investigation must not see each
other's identifiers, and ``ContextVar`` gives that per-task isolation for free.

Values must be snapshotted at record-creation time. The JSON formatter runs on
the ``QueueListener`` thread, which has no access to the emitting task's
context, so anything read here from a formatter would silently come out null.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

request_id: ContextVar[str | None] = ContextVar("vouch_request_id", default=None)
investigation_id: ContextVar[str | None] = ContextVar("vouch_investigation_id", default=None)
job_id: ContextVar[str | None] = ContextVar("vouch_job_id", default=None)
channel: ContextVar[str | None] = ContextVar("vouch_channel", default=None)

CONTEXT_VARS = {
    "request_id": request_id,
    "investigation_id": investigation_id,
    "job_id": job_id,
    "channel": channel,
}


def current_correlation() -> dict[str, str]:
    """Snapshot of the bound identifiers for the calling task."""
    return {name: var.get() for name, var in CONTEXT_VARS.items() if var.get() is not None}


@contextmanager
def bind_correlation(**fields: str | None) -> Iterator[None]:
    """Bind correlation identifiers for the duration of the block.

    Unknown names are ignored so a caller cannot invent identifiers the record
    factory does not know how to attach. Tokens are always reset, including when
    the body raises.
    """
    tokens = [(CONTEXT_VARS[n], CONTEXT_VARS[n].set(v)) for n, v in fields.items() if n in CONTEXT_VARS]
    try:
        yield
    finally:
        for var, token in tokens:
            var.reset(token)