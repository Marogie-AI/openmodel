import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PlanModel
from app.db.seed import PLAN_MODEL_ROWS, seed_plans

pytestmark = pytest.mark.db


async def test_seed_plans_is_idempotent(session: AsyncSession) -> None:
    await seed_plans(session)
    count = await session.scalar(select(func.count()).select_from(PlanModel))
    assert count == len(PLAN_MODEL_ROWS)


async def test_seed_plans_revokes_a_model_no_longer_in_the_plan(session: AsyncSession) -> None:
    session.add(PlanModel(plan_code="free", model_name="secret-model"))
    await session.commit()

    await seed_plans(session)

    models = (
        await session.scalars(select(PlanModel.model_name).where(PlanModel.plan_code == "free"))
    ).all()
    assert "secret-model" not in models
    assert set(models) == {
        row["model_name"] for row in PLAN_MODEL_ROWS if row["plan_code"] == "free"
    }
