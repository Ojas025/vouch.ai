from dataclasses import dataclass
import os

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
from vouch.tools.models import ToolDefinition, ToolSideEffects
from vouch.tools.rate_limit import RateLimiter
from vouch.tools.registry import ToolRegistry
from vouch.tools.tools.web import (
    ExtractPageContentArgs,
    ExtractPageContentOutput,
    ScreenshotPageArgs,
    ScreenshotPageOutput,
    WebInspectArgs,
    WebInspectOutput,
    extract_page_content,
    screenshot_page,
    web_inspect,
)

logger = get_logger(__file__)

REDIS_URL = os.getenv("REDIS_URL")
STREAM_KEY = os.getenv("JOB_QUEUE_STREAM_KEY", "vouch:jobs")
CONSUMER_GROUP = os.getenv("JOB_QUEUE_GROUP_NAME", "vouch-workers")

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
    stream = EventStream(redis_url=settings.redis_url, stream_name=STREAM_KEY)

    session_factory = SessionLocal
    leases = LeaseManager(session_factory)
    ledger = EvidenceLedger()
    tool_registry = ToolRegistry()
    rate_limiter = RateLimiter(max_tokens=30, refill_rate=0.5)
    tool_gateway = ToolGateway(tool_registry, rate_limiter)

    tool_registry.register(
        TOOL_DEFINITIONS
    )
    tool_registry.register([
        ToolDefinition(
            tool_name="web_inspect",
            description="Inspect a URL and follow HTTP redirects to record the hop chain and landing status.",
            handler=web_inspect,
            args_model=WebInspectArgs,
            output_model=WebInspectOutput,
            side_effects=ToolSideEffects.NETWORK,
            timeout_sec=15.0,
        ),
        ToolDefinition(
            tool_name="extract_page_content",
            description="Execute JavaScript and extract rendered Markdown text, titles, and hyperlinks from a webpage via Obscura.",
            handler=extract_page_content,
            args_model=ExtractPageContentArgs,
            output_model=ExtractPageContentOutput,
            side_effects=ToolSideEffects.NETWORK,
            timeout_sec=25.0,
        ),
        ToolDefinition(
            tool_name="screenshot_page",
            description="Render a webpage in a headless browser and capture a 1280x720 PNG screenshot artifact.",
            handler=screenshot_page,
            args_model=ScreenshotPageArgs,
            output_model=ScreenshotPageOutput,
            side_effects=ToolSideEffects.NETWORK,
            timeout_sec=25.0,
        ),
    ])

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
