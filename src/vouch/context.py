from dataclasses import dataclass

from redis.asyncio.client import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from vouch.agents.registry import TOOL_DEFINITIONS
from vouch.config import settings
from vouch.db.session import SessionLocal
from vouch.lease import LeaseManager
from vouch.ledger.ledger import EvidenceLedger
from vouch.logger import get_logger
from vouch.queue.stream import EventStream
from vouch.tools.gateway import ToolGateway
from vouch.tools.rate_limit import RateLimiter
from vouch.tools.registry import ToolRegistry

logger = get_logger(__file__)

@dataclass(frozen=True)
class WorkerContext:
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    stream: EventStream
    leases: LeaseManager
    ledger: EvidenceLedger
    tool_registry: ToolRegistry
    tool_gateway: ToolGateway
    rate_limiter: RateLimiter

async def init_worker_context() -> WorkerContext:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    stream = EventStream(redis_url=settings.redis_url, stream_name=settings.job_queue_stream_key)

    session_factory = SessionLocal
    leases = LeaseManager(session_factory)
    ledger = EvidenceLedger()
    tool_registry = ToolRegistry()
    rate_limiter = RateLimiter(max_tokens=30, refill_rate=0.5)
    tool_gateway = ToolGateway(tool_registry, rate_limiter)

    tool_registry.register(
        TOOL_DEFINITIONS
    )

    logger.info("worker context initialized", extra={"event": "worker.context_ready"})

    return WorkerContext(
        session_factory=session_factory,
        leases=leases,
        ledger=ledger,
        tool_registry=tool_registry,
        tool_gateway=tool_gateway,
        stream=stream,
        redis=redis,
        rate_limiter=rate_limiter
    )
