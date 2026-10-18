
import asyncio
from typing import Any, List

from pydantic import BaseModel
from vouch.tools.models import ToolDefinition

class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def resolve(self, tool_name: str) -> ToolDefinition | None:
        return self._tools.get(tool_name, None)

    def schemas_from_prompt(self) -> list[dict]:
        """Emit {name, description, tool_schema} for agent runtime injection"""
        schemas: list[dict] = []

        for tool in self._tools.values():
            schemas.append({
                "name": tool.tool_name,
                "description": tool.description,
                "tool_schema": tool.args_model,
            })

        return schemas

    def register(self, tools: List[ToolDefinition]):
        for tool in tools:
            if tool.tool_name in self._tools:
                raise ValueError(f"duplicate tool: {tool.tool_name}")

            self._tools[tool.tool_name] = tool

    async def invoke(self, tool_name: str, args: BaseModel) -> Any:
        return await self._tools[tool_name].handler(args)
