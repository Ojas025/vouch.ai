from datetime import timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import case, func, type_coerce, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.types import Interval
from vouch.db.models import Investigations
from vouch.db.session import SessionLocal
from vouch.lease import Lease
from vouch.logger import get_logger

logger = get_logger(__file__)

class ExecutionOutcome(StrEnum):
    COMPLETED = "completed"
    RETRY = "retry"
    DEAD = "dead"

class InvalidLease(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)

async def complete_investigation(result: Any, lease: Lease) -> ExecutionOutcome:
    """Persist investigation outcome."""
    now = func.clock_timestamp()

    try:
        async with SessionLocal() as session:
            async with session.begin():
                # validate lease
                stmt = (
                    update(Investigations)
                    .where(
                        Investigations.status == "processing",
                        Investigations.investigation_id == lease.investigation_id,
                        Investigations.lease_owner == lease.owner,
                        Investigations.fencing_token == lease.fencing_token,
                        Investigations.lease_expires_at > now
                    )
                    .values(
                        status = "completed",
                        completed_at = now,
                        lease_owner = None,
                        lease_expires_at = None,
                        lease_renewed_at = None,
                        result = result,
                        last_error = None,
                        not_before = None
                    ).returning(Investigations.status)
                )

                updated_status = (await session.execute(stmt)).scalar_one_or_none()

                if updated_status is None:
                    raise InvalidLease("Lease expired, lost, or no longer valid")

        return ExecutionOutcome.COMPLETED

    except InvalidLease:
        logger.warning(
            "Investigation lease lost: %s",
            lease.investigation_id
        )
        raise

    except SQLAlchemyError:
        logger.exception("Failed to persist invesigation result")
        raise

async def handle_failure(error: str, lease: Lease, retry_delay: float) -> ExecutionOutcome:
    """Persist investigation failure"""
    now = func.clock_timestamp()

    assert retry_delay > 0, "retry-delay must not be negative"
    retry_at = now + type_coerce(timedelta(seconds=retry_delay), type_=Interval())

    try:
        async with SessionLocal() as session:
            async with session.begin():
                stmt = (
                    update(Investigations)
                    .where(
                        Investigations.status == "processing",
                        Investigations.investigation_id == lease.investigation_id,
                        Investigations.lease_owner == lease.owner,
                        Investigations.fencing_token == lease.fencing_token,
                        Investigations.lease_expires_at > now
                    )
                    .values(
                        status = case(
                            (Investigations.attempts < Investigations.max_attempts, "retry"),
                            else_="dead"
                        ),
                        not_before = case(
                            (
                                Investigations.attempts < Investigations.max_attempts,
                                retry_at
                            ),
                            else_=None
                        ),
                        lease_owner = None,
                        lease_expires_at = None,
                        lease_renewed_at = None,
                        result = None,
                        last_error = error,
                    ).returning(Investigations.status)
                )

                updated_status = (await session.execute(stmt)).scalar_one_or_none()

                if updated_status is None:
                    raise InvalidLease("Lease expired, lost, or no longer valid")

        return ExecutionOutcome(updated_status)

    except InvalidLease:
        logger.warning(
            "Investigation lease lost: %s",
            lease.investigation_id
        )
        raise

    except SQLAlchemyError:
        logger.exception("Failed to persist investigation failure")
        raise
