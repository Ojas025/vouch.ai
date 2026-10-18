from collections import defaultdict

from pydantic import BaseModel, Field

class BudgetManager:
    def __init__(self):
        ...

    def try_consume_tool_call(self, investigation_id: str, agent_id: str):
        ...
