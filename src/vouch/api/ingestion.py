from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vouch.db.models import OutboxEvent, Submission
from vouch.logger import get_logger
from vouch.observability.propagation import capture_trace_context

logger = get_logger(__file__)

class IngestionResult(BaseModel):
    submission_id: str
    is_duplicate: bool = False

class WebhookIngestionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def ingest(self, channel: str, provider_account_id: str, provider_event_id, payload: dict, payload_digest: str) -> IngestionResult:
        """
            Transactional Outbox
            - Insert into Submission and OutboxEvent
            - A RelayService then pushes this to the Redis queue
        """
        async with self.session_factory() as session:
            async with session.begin():
                # Persist to Postgresql
                stmt = (
                    insert(Submission).values(
                        channel=channel,
                        provider_account_id=provider_account_id,
                        provider_event_id=provider_event_id,
                        payload=payload,
                        sha256_digest=payload_digest,
                        state="received",
                    ).on_conflict_do_nothing(
                        index_elements=[
                            Submission.channel,
                            Submission.provider_account_id,
                            Submission.provider_event_id
                        ]
                    ).returning(Submission.id)
                )

                submission_id = (
                    await session.execute(stmt)
                ).scalar_one_or_none()

                if submission_id is not None:
                    stmt = insert(OutboxEvent).values(
                        channel=channel,
                        submission_id=submission_id,
                        trace_context=capture_trace_context() or None,
                    ).returning(OutboxEvent.id)

                    event_id = (
                        await session.execute(stmt)
                    ).scalar_one_or_none()

                    is_duplicate = False
                    payload_matches = True
                else:
                    existing = (
                        await session.execute(
                            select(Submission.id, Submission.sha256_digest).where(
                                Submission.channel == channel,
                                Submission.provider_account_id == provider_account_id,
                                Submission.provider_event_id == provider_event_id
                            )
                        )
                    ).one_or_none()

                    if existing is None:
                        raise RuntimeError("Conflicting submission was not found")

                    submission_id = existing.index_element
                    is_duplicate = True
                    payload_matches = existing.sha256_digest == payload_digest

        # Transaction committed successfully

        if is_duplicate:
            logger.info(
                "duplciate submission",
                extra={
                    "event": "submission.duplicate",
                    "component": "api.ingestion",
                    "submission_id": str(submission_id),
                    "payload_bytes": len(str(payload)),
                    "outcome": "ok",
                },
            )

            if not payload_matches:
                logger.warning(
                    "provider event ID reused with different payload",
                    extra={
                        "event": "submission.payload_mismatch",
                        "component": "api.ingestion",
                        "submission_id": str(submission_id),
                        "channel": channel,
                        "provider_account_id": provider_account_id,
                    },
                )
        else:
            logger.info(
                "submission persisted",
                extra={
                    "event": "submission.persisted",
                    "component": "api.ingestion",
                    "job_id": str(event_id),
                    "submission_id": str(submission_id),
                    "payload_bytes": len(str(payload)),
                    "outcome": "ok",
                },
            )

        return IngestionResult(
            submission_id=str(submission_id),
            is_duplicate=submission_id is None
        )
