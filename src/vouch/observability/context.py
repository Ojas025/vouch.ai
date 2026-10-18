"""Per-task correlation identifiers for logs, audit events and spans.

ContextVars rather than a thread-local because the boundaries are asynchronous.
Snapshot at record creation: the JSON formatter runs where the context is gone.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

investigation_id: ContextVar[str | None] = ContextVar("vouch_investigation_id", default=None)
job_id: ContextVar[str | None] = ContextVar("vouch_job_id", default=None)

CONTEXT_VARS = {"investigation_id": investigation_id, "job_id": job_id}


def current_correlation() -> dict[str, str]:
    """Snapshot of the bound identifiers for the calling task."""
    return {name: var.get() for name, var in CONTEXT_VARS.items() if var.get() is not None}


@contextmanager
def bind_correlation(**fields: str | None) -> Iterator[None]:
    """Bind identifiers for the block; unknown names are ignored, tokens always reset."""
    tokens = [(CONTEXT_VARS[n], CONTEXT_VARS[n].set(v)) for n, v in fields.items() if n in CONTEXT_VARS]
    try:
        yield
    finally:
        for var, token in tokens:
            var.reset(token)