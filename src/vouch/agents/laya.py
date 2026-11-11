import copy
import time
from typing import Any, Literal, OrderedDict

import laya
from pydantic import BaseModel, ConfigDict, Field
from vouch.logger import get_logger
from vouch.observability.spans import span
from vouch.utils import RetryPolicy, _backoff, _is_retryable

logger = get_logger(__file__)

class LayaProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    decision_schema: type[BaseModel]

    batch_size: int = Field(default=32, ge=1)
    cache_size: int = Field(default=1024, ge=0) # 0 disables the cache
    cache_ttl_s: float = Field(default=300.0, gt=0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

class LayaError(RuntimeError):
    ...

Reason = Literal["invalid_input", "error", "low_confidence"]

class LayaResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    ok: bool
    value: Any = None # result of return_details=True
    reason: Reason | None = None
    error: str | None = None
    cached: bool = False

    def unwrap(self) -> Any:
        if not self.ok:
            raise LayaError(f"{self.reason}: {self.error}")
        return self.value

class _LRUCache:
    def __init__(self, size: int, ttl: float):
        self._size, self._ttl = size, ttl
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any | None:
        if not self._size:
            return None

        item = self._d.get(key)
        if item is None:
            return None

        expires, val = item
        if expires < time.monotonic():
            # already expired, invalidate from cache
            del self._d[key]
            return False

        # move recently accessed element to start
        self._d.move_to_end(key)
        return val

    def put(self, key: str, val: Any) -> None:
        if not self._size:
            return None

        self._d[key] = (time.monotonic() + self._ttl, val)
        self._d.move_to_end(key)

        # purge cache if size grows
        while len(self._d) > self._size:
            self._d.popitem(last=False)

def _short(e: BaseException) -> str:
    return f"{type(e).__name__: {e}}"[:200]

class LayaClient:
    """Reusable laya client for structured decisions.   """

    def __init__(self, profile: LayaProfile):
        self.profile = profile

        self.agent = laya.load("convaiinnovations/laya", subfolder="typed-decisions")
        self._cache = _LRUCache(self.profile.cache_size, self.profile.cache_ttl_s)

    def _fail(self, error: Exception, attempt: int, final: bool) -> None:
        logger.error(
            "laya decision failed",
            extra={
                "event": "laya.provider_error",
                "component": "agents.laya",
                "role": self.profile.role,
                "error_category": type(error).__name__,
                "attempt": attempt,
                "outcome": "failure",
                "final": final,
            },
            exc_info=error,
        )

    def decide_many(self, states: list[str]) -> list:
        results: list[LayaResult | None] = [None] * len(states)
        misses = []
        miss_indices = []

        with span("vouch.jev.decide", {"vouch.jev.role": self.profile.role}):
            for index, state in enumerate(states):
                if not isinstance(state, str) or not state.strip():
                    results[index] = LayaResult(ok=False, reason="invalid_input")

                key = f"{state}:{self.profile.decision_schema.__name__}"

                cached = self._cache.get(key)
                if cached is not None:
                    results[index] = LayaResult(ok=True, value=copy.deepcopy(cached))
                else:
                    misses.append(state)
                    miss_indices.append(index)

            if misses:
                policy = self.profile.retry_policy
                attempts = policy.max_attempts if policy.enabled else 1
                fresh = None

                for attempt in range(attempts):
                    with span(f"attempt.{attempt + 1}"):
                        try:
                            fresh = self.agent.decide_batch(misses, schema=self.profile.decision_schema)
                        except Exception as e:
                            if not _is_retryable(e):
                                self._fail(e, attempt + 1, True)
                                raise
                            if attempt == attempts - 1:
                                self._fail(e, attempt + 1, True)
                                raise
                            self._fail(e, attempt + 1, False)
                            time.sleep(_backoff(attempt, policy))
                            continue
                        break

                for index, val in zip(miss_indices, fresh):
                    results[index] = LayaResult(ok=True, value=copy.deepcopy(val))
                    self._cache.put(f"{states[index]}:{self.profile.decision_schema.__name__}", val)

        return results

    def decide(self, state: str) -> LayaResult:
        return self.decide_many([state])[0]
