from datetime import datetime, timedelta
import asyncio
from uuid import UUID

from pydantic.dataclasses import dataclass
from sqlalchemy import and_, or_, select, update, func
from vouch.db.models import Investigations
from vouch.logger import get_logger
from vouch.worker import RENEW_INTERVAL

logger = get_logger(__file__)
LEASE_TTL = timedelta(seconds=60)

class LeaseLost(Exception):
    """Renewal failed - worker is a zombie"""
    ...

class LeaseUnavailable(Exception):
    """Already acquired by a worker, duplicate delivery"""
    ...

@dataclass(frozen=True)
class Lease:
    investigation_id: UUID
    owner: UUID
    fencing_token: int
    expires_at: datetime

class LeaseManager:
    def __init__(self, session_factory, ttl: timedelta = LEASE_TTL):
        self.ttl = ttl
        self.session_factory = session_factory

    async def acquire(self, investigation_id: UUID, owner_id: UUID) -> Lease | None:
        """Acquire job for processing. Bump fencing token."""
        now = func.clock_timestamp()
        lease = None

        async with self.session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        update(Investigations)
                        .where(
                            Investigations.investigation_id == investigation_id,
                            Investigations.attempts < Investigations.max_attempts,
                            or_(
                                and_(
                                    Investigations.status == "received",
                                    Investigations.lease_owner.is_(None),
                                ),
                                and_(
                                    Investigations.status == "processing",
                                    Investigations.lease_expires_at <= now,
                                ),
                                and_(
                                    Investigations.status == "retry",
                                    Investigations.not_before <= now,
                                    Investigations.lease_owner.is_(None)
                                )
                            ),
                        )
                        .values(
                            status="processing",
                            lease_owner=owner_id,
                            lease_expires_at=now + LEASE_TTL,
                            lease_renewed_at=now,
                            fencing_token=Investigations.fencing_token + 1,
                            attempts=Investigations.attempts + 1
                        ).returning(Investigations.lease_owner, Investigations.lease_expires_at, Investigations.fencing_token)
                    )
                ).one_or_none()

        if row is not None:
            lease = Lease(
                investigation_id=investigation_id,
                owner=row.lease_owner,
                expires_at=row.lease_expires_at,
                fencing_token=row.fencing_token
            )

        return lease

    async def renew(self, lease: Lease) -> Lease | None:
        """
        Heartbeat sent by the owner to renew the token, if the expiry is not met.

        Conditions:
            - matching investigation_id
            - matching lease_owner
            - matching fencing_token
            - lease is not expired
        """

        now = func.clock_timestamp()
        renewed_lease = None

        async with self.session_factory() as session:
            async with session.begin():

                row = (
                    await session.execute(
                        update(Investigations).where(
                            and_(
                                Investigations.status == "processing",
                                Investigations.investigation_id == lease.investigation_id,
                                Investigations.lease_owner == lease.owner,
                                Investigations.fencing_token == lease.fencing_token,
                                Investigations.lease_expires_at > now
                            )
                        ).values(
                            lease_expires_at=now + LEASE_TTL,
                            lease_renewed_at=now,
                        ).returning(Investigations.lease_expires_at, Investigations.fencing_token)
                    )
                ).one_or_none()

        if row is not None:
            renewed_lease = Lease(
                investigation_id=lease.investigation_id,
                owner=lease.owner,
                expires_at=row.lease_expires_at,
                fencing_token=row.fencing_token
            )

        return renewed_lease

    def _verify_lease(self, lease: Lease, investigation) -> bool:
        if lease.owner != investigation.lease_owner:
            return False

        if lease.fencing_token != investigation.fencing_token:
            return False

        return True

    async def release(self, lease: Lease) -> None:
        """
        Release acquired resource.

        Conditions:
            - A worker completes processing for a task with either success/failure
            - A worker loses it's lease and another worker picks this job up
        """
        now = func.clock_timestamp()

        async with self.session_factory() as session:
            async with session.begin():
                row = (
                    await session.execute(
                        select(Investigations.investigation_id, Investigations.lease_owner, Investigations.fencing_token, Investigations.status).where(
                            Investigations.investigation_id == lease.investigation_id
                        )
                    )
                ).one_or_none()

                if row is None:
                    raise RuntimeError(f"investigation {lease.investigation_id} not found.")

                if not self._verify_lease(lease, row):
                    raise RuntimeError("Lease verification failed.")

                (
                    await session.execute(
                        update(Investigations).where(
                            Investigations.investigation_id == lease.investigation_id
                        ).values(
                            status = "retry",
                            not_before = now + 30.0,
                            lease_owner = None,
                            lease_expires_at = None,
                            lease_renewed_at = None
                        )
                    )
                )

class LeaseKeeper:
    """Background heartbeat manager which renews the acquired lease as long as the worker needs it."""

    def __init__(self, lease_manager: LeaseManager, lease: Lease):
        self.lease_manager = lease_manager
        self._lease = lease

        self._stop_event = asyncio.Event()
        self._lost_event = asyncio.Event()
        self._task = None

    def start(self):
        """Start the heartbeat loop."""
        if self._task is not None:
            raise RuntimeError("Leasekeeper has already been started.")

        self._task = asyncio.create_task(
            self._heartbeat_loop()
        )

    async def _heartbeat_loop(self):
        renewed_lease = None

        while not self._stop_event.is_set():
            try:
                # Only stop when either the task is completed or worker dies
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=RENEW_INTERVAL.total_seconds()
                )
                return # stop event was triggered

            except TimeoutError:
                pass

            try:
                renewed_lease = await self.lease_manager.renew(self._lease)

            except asyncio.CancelledError:
                raise
            except Exception:
                self._lost_event.set()

            if renewed_lease is None:
                self._lost_event.set()
                return

            self._lease = renewed_lease


    async def stop(self):
        """Stop the heartbeat loop."""
        self._stop_event.set()

        if self._task is not None:
            await self._task

    async def wait_for_loss(self):
        """Waits for lease ownership loss and notifies worker"""
        await self._lost_event.wait()

    @property
    def lease(self) -> Lease:
        return self._lease
