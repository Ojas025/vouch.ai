from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import func, update
from vouch.db.models import InvestigationToolBudgets, Investigations

class BudgetExceeded(Exception):
    pass

class BudgetManager:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]):
        self.session_factory = session_factory

    async def try_consume(self, investigation_id: str, tool_name: str, limit: int) -> bool:
        if limit <= 0:
            return False

        async with self.session_factory() as session:
            async with session.begin():
                try:
                    # Check tool budget
                    investigation_budget = (
                        await session.execute(
                            update(Investigations).where(
                                Investigations.investigation_id == investigation_id,
                                Investigations.tool_calls_used < Investigations.tool_call_limit
                            ).values(
                                tool_calls_used=Investigations.tool_calls_used + 1
                            ).returning(Investigations.tool_calls_used)
                        )
                    ).scalar_one_or_none()

                    if investigation_budget is None:
                        raise BudgetExceeded

                    tool_budget = (
                        await session.execute(
                            insert(InvestigationToolBudgets).values(
                                investigation_id=investigation_id,
                                tool_name=tool_name,
                                tool_calls_used=1
                            ).on_conflict_do_update(
                                index_elements=[InvestigationToolBudgets.investigation_id, InvestigationToolBudgets.tool_name],
                                set_={
                                    "calls_used": InvestigationToolBudgets.tool_calls_used + 1,
                                    "updated_at": func.now()
                                },
                                where=InvestigationToolBudgets.tool_calls_used < limit
                            ).returning(InvestigationToolBudgets.tool_calls_used)
                        )
                    ).scalar_one_or_none()

                    if tool_budget is None:
                        # budget exceeded, halt the investigation
                        raise BudgetExceeded

                    return True

                except BudgetExceeded:
                    return True
