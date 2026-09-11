import asyncio
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, text

from alembic import command
from app.config import settings
from app.db.models import Base
from app.db.session import make_engine
from tests.conftest import TABLES, alembic_config

pytestmark = pytest.mark.db

EXPECTED_TABLES = {name.strip() for name in TABLES.split(",")}


def metadata_diff(connection: Connection) -> list[object]:
    context = MigrationContext.configure(connection, opts={"compare_type": True})
    return list(compare_metadata(context, Base.metadata))


@pytest.fixture
async def migrated() -> None:
    """Drop every table, then run `alembic upgrade head` from scratch."""
    engine = make_engine(settings.database_owner_url)
    async with engine.begin() as connection:
        await connection.execute(text(f"DROP TABLE IF EXISTS {TABLES}, alembic_version CASCADE"))
    await engine.dispose()
    await asyncio.to_thread(command.upgrade, alembic_config(), "head")


async def test_upgrade_creates_the_schema_and_seeds_plans(migrated: None) -> None:
    engine = make_engine(settings.database_owner_url)
    try:
        async with engine.connect() as connection:
            tables = set(
                (
                    await connection.execute(
                        text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                    )
                )
                .scalars()
                .all()
            )
            assert tables >= EXPECTED_TABLES
            plans = await connection.execute(text("SELECT count(*) FROM plan"))
            assert plans.scalar_one() == 2
            models = await connection.execute(text("SELECT count(*) FROM plan_model"))
            assert models.scalar_one() == 5
            assert await connection.run_sync(metadata_diff) == []
    finally:
        await engine.dispose()


async def test_app_role_can_insert_into_organization(migrated: None) -> None:
    """The owner's default privileges reach freshly migrated tables. Rolled back, not kept."""
    engine = make_engine(settings.database_url)
    try:
        async with engine.connect() as connection:
            await connection.execute(
                text("INSERT INTO organization (id, name, plan_code) VALUES (:id, :name, 'free')"),
                {"id": uuid4(), "name": f"org-{uuid4()}"},
            )
            await connection.rollback()
    finally:
        await engine.dispose()
