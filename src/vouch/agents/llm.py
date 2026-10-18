from collections.abc import Sequence
from typing import Any

import os
from langchain_core.messages import AIMessageChunk
from langchain_core.runnables import Runnable
from langchain_litellm import ChatLiteLLM
from litellm import AsyncIterator
from vouch.agents.models import LLMProfile

class LLMClient:
    def __init__(self, profile: LLMProfile):
        params: dict[str, Any] = {
            "model": profile.name,
            "request_timeout": profile.timeout_sec,
            "max_retries": profile.max_retries,
            "model_kwargs": profile.model_kwargs,
            "temperature": profile.temperature,
            "max_tokens": profile.max_tokens
        }

        if profile.base_url is not None:
            params["api_base"] = profile.base_url

        if profile.api_key is not None:
            api_key = os.getenv(profile.api_key)
            if not api_key:
                raise ValueError(
                    f"Missing environment variable: "
                    f"{profile.api_key}"
                )

            params["api_key"] = api_key

        self._llm = ChatLiteLLM(**params)

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable:
        return self._llm.with_structured_output(schema, **kwargs)

    async def ainvoke(self, messages: Any, **kwargs: Any):
        return await self._llm.ainvoke(messages, **kwargs)

    async def stream(self, messages: Any, **kwargs: Any) -> AsyncIterator[AIMessageChunk]:
        async for chunk in self._llm.astream(messages, **kwargs):
            yield chunk

    def bind_tools(self,tools: Sequence[Any], **kwargs: Any) -> Runnable:
        return self._llm.bind_tools(list(tools), **kwargs)
