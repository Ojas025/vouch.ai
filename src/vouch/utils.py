import asyncio
import httpx
from functools import wraps
from pathlib import Path
from random import uniform
import uuid
import platform
import time
from pydantic import BaseModel


class RetryPolicy(BaseModel):
    enabled: bool = False
    max_attempts: int = 3
    base_delay: int = 1
    max_delay: int = 10


def _backoff(attempt: int, policy: RetryPolicy):
    return uniform(0, min(policy.max_delay, policy.base_delay * (2 ** attempt)))


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    return bool(getattr(error, "is_retryable", False))

def retry(max_attempts=3, base_delay=1, max_delay=10):
    if max_attempts < 1:
        raise ValueError(f"max_attempts must be >= 1, got {max_attempts}")

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except httpx.TransportError:
                    if attempt == max_attempts - 1:
                        raise

                    delay = min(max_delay, base_delay * (2 ** attempt))
                    await asyncio.sleep(uniform(delay / 2, delay))

        return wrapper

    return decorator

def uuid7() -> uuid.UUID:
    # RFC 9562 layout: 48-bit ms timestamp, version 7, variant 0b10, random tail.
    ts_ms = int(time.time() * 1000)
    rand = uuid.uuid4().int
    value = (ts_ms << 80) | (7 << 76) | (rand & ((1 << 76) - 1))
    value = (value & ~(0b11 << 62)) | (0b10 << 62)
    return uuid.UUID(int=value)

def delete_from_path(path: Path):
    path.unlink(missing_ok=True)

def get_platform() -> tuple[str, str]:
    os = platform.system().lower()
    machine = platform.machine().lower()

    arch_map = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "x64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }

    arch = arch_map[machine]

    if not os or not arch:
        raise RuntimeError(f"Unsupported platform: {os}/{machine}")

    key = (os, arch)

    return key
