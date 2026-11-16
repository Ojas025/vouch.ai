import copy
import time
from typing import Any, Literal, OrderedDict

import laya
from pydantic import BaseModel, ConfigDict, Field, model_validator
from vouch.logger import get_logger
from vouch.observability.spans import span
from vouch.utils import RetryPolicy, _backoff, _is_retryable

logger = get_logger(__file__)

_CACHE_MISS = object()

class LayaProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    decision_schema: type[BaseModel]

    batch_size: int = Field(default=32, ge=1)
    cache_size: int = Field(default=1024, ge=0) # 0 disables the cache
    cache_ttl_s: float = Field(default=300.0, gt=0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    @model_validator(mode="after")
    def valid_retry_policy(self) -> "LayaProfile":
        if self.retry_policy.enabled and self.retry_policy.max_attempts < 1:
            raise ValueError("retry_policy.max_attempts must be >= 2 if retry_policy.enabled is True")
        return self

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
        self._size = size
        self._ttl = ttl
        self._d: OrderedDict[str, tuple[float, Any]] = OrderedDict()

    def get(self, key: str) -> Any:
        if not self._size:
            return _CACHE_MISS

        item = self._d.get(key)
        if item is None:
            return _CACHE_MISS

        expires, val = item
        if expires < time.monotonic():
            # already expired, invalidate from cache
            del self._d[key]
            return _CACHE_MISS

        # move recently accessed element to start
        self._d.move_to_end(key)
        return val

    def put(self, key: str, val: Any) -> None:
        if not self._size:
            return

        self._d[key] = (time.monotonic() + self._ttl, val)
        self._d.move_to_end(key)

        # purge cache if size grows
        while len(self._d) > self._size:
            self._d.popitem(last=False)

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

    def _normalize_value(self, value: Any) -> BaseModel:
        if isinstance(value, BaseModel):
            raw =  value.model_dump()
        elif isinstance(value, dict):
            raw = value
        else:
            raise LayaError(f"Unexpected decision value type: {type(value)}")

        validated = self.profile.decision_schema.model_validate(raw)
        return validated

    def decide_many(self, states: list[str]) -> list[LayaResult]:
        results: list[LayaResult | None] = [None] * len(states)
        misses = []
        miss_indices = []

        with span("vouch.jev.decide", {"vouch.jev.role": self.profile.role}):
            for index, state in enumerate(states):
                if not isinstance(state, str) or not state.strip():
                    results[index] = LayaResult(ok=False, reason="invalid_input")
                    continue

                key = f"{state}:{self.profile.decision_schema.__name__}"

                cached = self._cache.get(key)
                if cached is not _CACHE_MISS:
                    results[index] = LayaResult(ok=True, value=copy.deepcopy(cached), cached=True)
                    continue

                misses.append(state)
                miss_indices.append(index)

            if misses:
                policy = self.profile.retry_policy
                attempts = policy.max_attempts if policy.enabled else 1
                fresh: list[Any] | None = None

                for attempt in range(attempts):
                    with span(f"attempt.{attempt + 1}"):
                        try:
                            fresh = self.agent.decide_batch(
                                misses,
                                schema=self.profile.decision_schema,
                                batch_size=self.profile.batch_size
                            )

                        except Exception as e:
                            retryable = _is_retryable(e)
                            final = not retryable or attempt == attempts - 1

                            self._fail(e, attempt + 1, final)
                            if final:
                                raise

                            time.sleep(_backoff(attempt, policy))
                            continue
                        break

                if fresh is None:
                    raise LayaError("Laya produced no result after all attempts")

                fresh = list(fresh)
                if len(fresh) != len(misses):
                    raise LayaError(f"Laya returned a different number of decisions than requested: {len(fresh)} != {len(misses)}")

                for index, raw_value in zip(miss_indices, fresh, strict=True):
                    value = self._normalize_value(raw_value)
                    results[index] = LayaResult(ok=True, value=copy.deepcopy(value), cached=False)

                    key = f"{states[index]}:{self.profile.decision_schema.__name__}"
                    self._cache.put(key, value)

        # A none value should never be returned to an agent
        if any(result is None for result in results):
            raise LayaError("Laya did not return a result for all states")

        return [result for result in results if result is not None]

    def decide(self, state: str) -> LayaResult:
        return self.decide_many([state])[0]
