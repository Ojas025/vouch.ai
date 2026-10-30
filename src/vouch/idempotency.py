from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import time
from typing import Awaitable, Callable, Optional, Tuple

from redis import Redis
import json

class IdempotencyStatus(StrEnum):
    NEW = "new"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class IdempotencyRecord:
    key: str
    status: IdempotencyStatus
    result: Optional[dict]
    created_at: float
    completed_at: Optional[float]
    error: Optional[str]

class IdempotencyStore:
    """Redis-based idempotency key storage"""

    def __init__(self, redis: Redis, key_prefix: str = "idempotency", ttl: int = 86400):
        self.redis = redis
        self.key_prefix = key_prefix
        self.ttl = ttl

    def _make_key(self, idempotency_key: str) -> str:
        return f"{self.key_prefix}:{idempotency_key}"

    def check_and_start(self, idempotency_key: str) -> Tuple[bool, Optional[IdempotencyRecord]]:
        """
            Check if the incoming request is new, mark as processing and return existing record.

            Returns:
                (is_new, existing_record) - True if new request, None if new
        """
        key = self._make_key(idempotency_key)
        now = time.monotonic()

        lua_script = """
        local existing = redis.call('GET', KEYS[1])

        if existing then
            return existing
        end

        local record = cjson.encode({
            status = 'processing'
            created_at = tonumber(ARGV[1]),
            completed_at = nil,
            result = nil,
            error = nil,
        })

        redis.call('SET', KEYS[1], record, 'EX', ARGV[2])

        return nil
        """

        result = self.redis.eval(lua_script, 1, key, now, self.ttl)

        if result is None:
            return True, None # New request

        # Duplicate request, existing record found
        data = json.loads(result)

        return False, IdempotencyRecord(
            key=key,
            status=IdempotencyStatus(data['status']),
            created_at=data.get('created_at'),
            completed_at=data.get('completed_at'),
            error=data.get('error'),
            result=data.get('result')
        )

    def complete(self, idempotency_key: str, result: dict):
        """Mark request as completed with a result"""

        key = self._make_key(idempotency_key)
        existing = self.get(key)
        now = time.monotonic()

        record = {
            'status': 'completed',
            'created_at': existing.created_at if existing else now,
            'completed_at': now,
            'result': result,
            'error': None
        }

        self.redis.set(key, json.dumps(record), ex=self.ttl)

    def reclaim_processing(self, idempotency_key: str, retry_processing_after: int) -> bool:
        """Atomically reclaim a stale processing record."""

        key = self._make_key(idempotency_key)
        now = time.monotonic()

        lua_script = """
        local existing = redis.call('GET', KEYS[1])
        if not existing then
            return 0
        end

        local record = cjson.decode(existing)
        if record['status'] ~= 'processing' then
            return 0
        end

        local created_at = tonumber(record['created_at'])
        if not created_at or (created_at + tonumber(ARGV[1])) > tonumber(ARGV[2]) then
            return 0
        end

        local new_record = cjson.encode({
            status = 'processing',
            created_at = tonumber(ARGV[2]),
            completed_at = nil,
            result = nil,
            error = nil
        })
        redis.call('SET', KEYS[1], new_record, 'EX', ARGV[3])
        return 1
        """

        return self.redis.eval(
            lua_script, 1, key, retry_processing_after, now, self.ttl
        ) == 1

    def fail(self, idempotency_key: str, error: str):
        """Mark request as failed."""
        key = self._make_key(idempotency_key)
        existing = self.get(idempotency_key)
        now = time.monotonic()

        record = {
            'status': 'failed',
            'created_at': existing.created_at if existing else now,
            'completed_at': now,
            'result': None,
            'error': error
        }

        self.redis.set(key, json.dumps(record), ex=self.ttl)

    def get(self, idempotency_key: str) -> Optional[IdempotencyRecord]:
        """Get existing idempotency record."""
        key = self._make_key(idempotency_key)
        data = self.redis.get(key)

        if not data:
            return None

        parsed = json.loads(data)
        return IdempotencyRecord(
            key=idempotency_key,
            status=IdempotencyStatus(parsed['status']),
            result=parsed.get('result'),
            created_at=parsed['created_at'],
            completed_at=parsed.get('completed_at'),
            error=parsed.get('error')
        )

    def delete(self, idempotency_key: str) -> bool:
        """Delete an idempotency record."""
        key = self._make_key(idempotency_key)
        return self.redis.delete(key) > 0

class  IdempotentOperation:
    """Wrapper for making operations idempotent"""

    def __init__(self, store: IdempotencyStore, retry_after: int = 60):
        self.store = store
        self.retry_after = retry_after

    def execute(self, idempotency_key: str, operation: Callable[..., dict], *args, **kwargs):
        """
        Execute synchronous operations idempotently

        Returns:
            (result, was_cached) - Result and whether it came from the cache
        """

        is_new, existing = self.store.check_and_start(idempotency_key)

        if not is_new:
            # Request already seen
            assert existing is not None, "Existing record cannot be None"

            if existing.status == IdempotencyStatus.COMPLETED:
                return existing.result, True

            elif existing.status == IdempotencyStatus.PROCESSING:
                # Check if it's stuck
                age = time.monotonic() - existing.created_at
                if age > self.retry_after:
                    # Retry if this worker automatically reclaims the key
                    if self.store.reclaim_processing(idempotency_key, self.retry_after):
                        is_new = True
                    else:
                        latest = self.store.get(idempotency_key)

                        if latest and latest.status == IdempotencyStatus.COMPLETED:
                            return latest.result, True

                        raise IdempotencyConflictError(
                            "Request is being processed",
                            retry_after=self.retry_after
                        )
                else:
                    raise IdempotencyConflictError(
                        "Request is being processed",
                        retry_after=self.retry_after
                    )

            elif existing.status == IdempotencyStatus.FAILED:
                raise IdempotencyFailureError(
                    f"Previous request failed: {existing.error}"
                )

        # Execute operation
        try:
            result = operation(*args, **kwargs)
            self.store.complete(idempotency_key, result)

            return result, False

        except Exception as e:
            self.store.fail(idempotency_key, str(e))
            raise

    async def aexecute(self, idempotency_key: str, operation, *args, **kwargs):
        """
        Execute asynchronous operations idempotently

        Returns:
            (result, was_cached) - Result and whether it came from the cache
        """

        is_new, existing = self.store.check_and_start(idempotency_key)

        if not is_new:
            # Request already seen
            assert existing is not None, "Existing record cannot be None"

            if existing.status == IdempotencyStatus.COMPLETED:
                return existing.result, True

            elif existing.status == IdempotencyStatus.PROCESSING:
                # Check if it's stuck
                age = time.monotonic() - existing.created_at
                if age > self.retry_after:
                    # Retry if this worker automatically reclaims the key
                    if self.store.reclaim_processing(idempotency_key, self.retry_after):
                        is_new = True
                    else:
                        latest = self.store.get(idempotency_key)

                        if latest and latest.status == IdempotencyStatus.COMPLETED:
                            return latest.result, True

                        raise IdempotencyConflictError(
                            "Request is being processed",
                            retry_after=self.retry_after
                        )
                else:
                    raise IdempotencyConflictError(
                        "Request is being processed",
                        retry_after=self.retry_after
                    )

            elif existing.status == IdempotencyStatus.FAILED:
                raise IdempotencyFailureError(
                    f"Previous request failed: {existing.error}"
                )

        # Execute operation
        try:
            result = await operation(*args, **kwargs)
            self.store.complete(idempotency_key, result)

            return result, False

        except Exception as e:
            self.store.fail(idempotency_key, str(e))
            raise


class IdempotencyConflictError(Exception):
    def __init__(self, message, retry_after: int):
        super().__init__(message)
        self.retry_after = retry_after

class IdempotencyFailureError(Exception):
    pass
