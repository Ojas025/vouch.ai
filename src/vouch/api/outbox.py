import asyncio

from datetime import datetime, timezone
from asyncio import sleep

from sqlalchemy import select

from vouch.config import settings
from vouch.db.models import OutboxEvent
from vouch.db.session import SessionLocal
from vouch.logger import configure_logging, get_logger
from vouch.queue.stream import EventStream

logger = get_logger(__name__)


async def publish_outbox(session, stream: EventStream):
    result = await session.execute(
        select(OutboxEvent)
        .where(OutboxEvent.status == "pending")
        .order_by(OutboxEvent.created_at)
        .limit(10)
        .with_for_update(skip_locked=True)
    )

    events = result.scalars().all()
    for row in events:
        event = {
            "submission_id": str(row.submission_id),
            "channel": row.channel,
        }

        carrier = getattr(row, "trace_context", None)
        if isinstance(carrier, dict) and carrier:
            await stream.produce(event, trace_context=carrier)
        else:
            await stream.produce(event)

        row.status = "published"
        row.published_at = datetime.now(timezone.utc)

        logger.info(
            "outbox event published",
            extra={
                "event": "outbox.published",
                "component": "api.outbox",
                "job_id": str(row.id),
                "submission_id": str(row.submission_id),
                "channel": row.channel,
                "outcome": "ok",
            },
        )
    await session.commit()

async def outbox_worker(stream: EventStream):
    while True:
        async with SessionLocal() as session:
            await publish_outbox(session, stream)

        await sleep(5)


async def main() -> None:
    """Entrypoint for the relay process that drains the outbox."""
    configure_logging()
    stream = EventStream(
        redis_url=settings.redis_url,
        stream_name=settings.events_stream,
    )

    await outbox_worker(stream)


if __name__ == "__main__":
    asyncio.run(main())
