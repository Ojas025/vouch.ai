from datetime import datetime, timezone
from asyncio import sleep

from sqlalchemy import select

from vouch.db.models import OutboxEvent
from vouch.db.session import SessionLocal
from vouch.queue.stream import EventStream


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

    for event in events:
        await stream.produce(event)

        event.status = "published"
        event.published_at = datetime.now(timezone.utc)

    await session.commit()

async def outbox_worker(stream: EventStream):
    while True:
        async with SessionLocal() as session:
            await publish_outbox(session, stream)

        await sleep(2)
