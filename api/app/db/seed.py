"""Plan reference data. Single source of truth for migration 0002 and the test fixtures."""

from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Plan, PlanModel

PLAN_ROWS: list[dict[str, Any]] = [
    {"code": "free", "requests_per_minute": 60, "max_concurrency": 2},
    {"code": "pro", "requests_per_minute": 600, "max_concurrency": 10},
]

PLAN_MODEL_ROWS: list[dict[str, Any]] = [
    {"plan_code": "free", "model_name": "qwen2.5:0.5b"},
    {"plan_code": "free", "model_name": "nomic-embed-text"},
    {"plan_code": "pro", "model_name": "qwen2.5:0.5b"},
    {"plan_code": "pro", "model_name": "nomic-embed-text"},
    {"plan_code": "pro", "model_name": "qwen2.5-coder:0.5b"},
]


async def seed_plans(session: AsyncSession) -> None:
    """Insert the plan reference rows, updating limits if they already exist."""
    plan = insert(Plan).values(PLAN_ROWS)
    await session.execute(
        plan.on_conflict_do_update(
            index_elements=[Plan.code],
            set_={
                "requests_per_minute": plan.excluded.requests_per_minute,
                "max_concurrency": plan.excluded.max_concurrency,
            },
        )
    )
    await session.execute(
        insert(PlanModel)
        .values(PLAN_MODEL_ROWS)
        .on_conflict_do_nothing(index_elements=[PlanModel.plan_code, PlanModel.model_name])
    )
    await session.commit()
