import asyncio
from functools import wraps
from pathlib import Path
from random import uniform
import uuid

def retry(max_attempts=3, base_delay=1, max_delay=10):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_attempts):
                try:
                    return await func(*args, **kwargs)
                except Exception:
                    if attempt == max_attempts - 1:
                        raise

                    delay = min(max_delay, base_delay * (2 ** attempt))
                    await asyncio.sleep(uniform(0, delay))

        return wrapper

    return decorator

def uuid7() -> uuid.UUID:
    return uuid.uuid4()

def delete_from_path(path: Path):
    if path.exists():
        path.unlink(missing_ok=True)
