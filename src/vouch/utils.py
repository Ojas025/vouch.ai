import asyncio
import httpx
from functools import wraps
from pathlib import Path
from random import uniform
import uuid

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
    return uuid.uuid4()

def delete_from_path(path: Path):
    path.unlink(missing_ok=True)
