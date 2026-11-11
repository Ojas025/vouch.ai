from calendar import timegm
from pathlib import Path
import random
from time import time
from typing import Any
import os
import asyncio
from email.utils import parsedate

from langchain_core.exceptions import ModelError
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.runnables import Runnable
from langchain_litellm import ChatLiteLLM
from pydantic import BaseModel, ConfigDict, Field
from vouch.agents.models import Capability
from vouch.agents.utils import load_prompt
from vouch.logger import get_logger
from vouch.observability.spans import ATTR_LLM_MODEL, SPAN_LLM_GENERATE, span

logger = get_logger(__name__)

class RetryPolicy(BaseModel):
    enabled: bool = False
    max_attempts: int = 3
    base_delay: int = 1
    max_delay: int = 10

class LLMProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    base_url: str | None = None
    api_key: str | None = None

    capabilities: list[Capability] = Field(default_factory=list)
    timeout_sec: float = Field(default=60, gt=0)
    max_retries: int = Field(default=3, ge=0, le=5)
    model_kwargs: dict[str, Any] = Field(default_factory=dict)

    temperature: float = 1.0
    max_tokens: int = 1000

    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

def _extract_retry_after(e: Exception) -> float | None:
    """Parse retry-after value from ModelError"""

    response = getattr(e, "response", None)
    if response is None:
        return None

    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if not retry_after:
        return None

    retry_after = retry_after.strip()
    if retry_after.isdigit():
        return float(retry_after)

    t = parsedate(retry_after)
    if t is None:
        return None

    return max(0.0, timegm(t[:6]) - time())

def _backoff(attempt: int, policy: RetryPolicy):
    return random.uniform(0, min(policy.max_delay, policy.base_delay * (2 ** attempt)))


def _is_retryable(error: Exception) -> bool:
    if isinstance(error, ModelError):
        return bool(getattr(error, "is_retryable", False))
    if isinstance(error, (ConnectionError, TimeoutError)):
        return True
    return False

class LLMClient:
    def __init__(self, profile: LLMProfile, system_prompt: Path | str | None = None, output_model: type[BaseModel] | None = None):
        provider_max_retries = 0 if profile.retry_policy.enabled else profile.max_retries
        params: dict[str, Any] = {
            "model": profile.model,
            "request_timeout": profile.timeout_sec,
            "max_retries": provider_max_retries,
            "model_kwargs": profile.model_kwargs,
            "temperature": profile.temperature,
            "max_tokens": profile.max_tokens,
        }

        if profile.base_url:
            params["api_base"] = profile.base_url

        if profile.api_key not in (None, "", "not-required"):
            api_key = os.getenv(profile.api_key)
            if not api_key:
                raise ValueError(
                    f"Missing environment variable: "
                    f"{profile.api_key}"
                )

            params["api_key"] = api_key

        self.profile = profile
        self.output_model = output_model

        if isinstance(system_prompt, Path):
            self.system_prompt = load_prompt(system_prompt)
        elif isinstance(system_prompt, str):
            self.system_prompt = system_prompt

        if not self.system_prompt.strip():
            raise ValueError("system_prompt cannot be empty")

        self.base_model = ChatLiteLLM(**params)

        model_runnable: Runnable = self.base_model
        if self.output_model is not None:
            model_runnable = self.base_model.with_structured_output(self.output_model)

        if self.system_prompt is not None:
            prompt = ChatPromptTemplate.from_messages([
                ("system", self.system_prompt),
                MessagesPlaceholder(variable_name="messages"),
            ])
            self.runnable: Runnable = prompt | model_runnable

    def _fail(self, error: Exception, attempt: int, final: bool) -> None:
        logger.error(
            "llm generation failed",
            extra={
                "event": "llm.provider_error",
                "component": "agents.llm",
                "model": self.profile.model,
                "error_category": type(error).__name__,
                "attempt": attempt,
                "outcome": "failure",
                "final": final,
            },
            exc_info=error,
        )

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        policy: RetryPolicy = self.profile.retry_policy
        attempts = policy.max_attempts if policy.enabled else 1
        input_data = {"messages": messages}

        with span(SPAN_LLM_GENERATE, {ATTR_LLM_MODEL: self.profile.model}):
            for attempt in range(attempts):
                with span(f"attempt.{attempt + 1}") as child:
                    try:
                        result = await self.runnable.ainvoke(input_data, **kwargs)
                        child.set_attribute("attempt", attempt + 1)
                        return result
                    except Exception as e:
                        child.set_attribute("error", type(e).__name__)
                        child.record_exception(e)

                        if not _is_retryable(e):
                            self._fail(e, attempt + 1, True)
                            raise

                        if attempt == attempts - 1:
                            self._fail(e, attempt + 1, True)
                            raise

                        wait = _extract_retry_after(e)
                        if wait is None:
                            wait = _backoff(attempt, policy)

                await asyncio.sleep(wait)

    async def stream(self, messages: Any, **kwargs: Any):
        policy: RetryPolicy = self.profile.retry_policy
        attempts = policy.max_attempts if policy.enabled else 1
        input_data = {"messages": messages}

        with span(SPAN_LLM_GENERATE, {ATTR_LLM_MODEL: self.profile.model}):
            for attempt in range(attempts):
                started = False
                with span(f"attempt.{attempt + 1}") as child:
                    try:
                        child.set_attribute("attempt", attempt + 1)
                        async for chunk in self.runnable.astream(input_data, **kwargs):
                            started = True
                            yield chunk
                        return
                    except Exception as e:
                        child.set_attribute("error", type(e).__name__)
                        child.record_exception(e)

                        if not _is_retryable(e):
                            self._fail(e, attempt + 1, True)
                            raise

                        if started or attempt == attempts - 1:
                            self._fail(e, attempt + 1, True)
                            raise

                        wait = _extract_retry_after(e)
                        if wait is None:
                            wait = _backoff(attempt, policy)

                await asyncio.sleep(wait)

    def get_model(self) -> ChatLiteLLM:
        return self.base_model

    def get_system_prompt(self) -> str:
        return self.system_prompt

    def get_output_model(self) -> type[BaseModel] | None:
        return self.output_model
