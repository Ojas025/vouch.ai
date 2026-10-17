import asyncio
import os

from datetime import datetime, timezone
from asyncio import sleep

from sqlalchemy import select

from vouch.db.models import OutboxEvent
from vouch.db.session import SessionLocal
from vouch.ingestion import Event
from vouch.stream import EventStream


async def publish_outbox(session, stream: EventStream):
    result = await session.execute(
        select(OutboxEvent)
        .where(OutboxEvent.status == "pending")
        .order_by(OutboxEvent.created_at)
        .limit(10)
        .with_for_update(skip_locked=True)
    )

    # fetch all result ORMs into a list
    events = result.scalars().all()

    for row in events:
        # ORM rows are not JSON serializable: the broker contract is the typed
        # Event (channel, idempotency_key, payload).
        await stream.produce(
            Event(
                channel=row.channel,
                idempotency_key=row.idempotency_key,
                payload=row.payload,
            )
        )

        row.status = "published"
        row.published_at = datetime.now(timezone.utc)

    await session.commit()

async def outbox_worker(stream: EventStream):
    while True:
        async with SessionLocal() as session:
            await publish_outbox(session, stream)

        await sleep(2)

# Must match the stream the API producer writes to in vouch.api.server.
EVENTS_STREAM = "events:stream"


async def main() -> None:
    """Entrypoint for the relay process that drains the outbox."""
    stream = EventStream(
        redis_url=os.environ["REDIS_URL"],
        stream_name=EVENTS_STREAM,
    )

    await outbox_worker(stream)


if __name__ == "__main__":
    asyncio.run(main())
