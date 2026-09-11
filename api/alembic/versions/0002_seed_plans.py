"""seed plans

The plan catalogue changes through a NEW migration, never by editing this one: the rows here
are history for every database that has already run it.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-11 11:45:35.261234

"""

import sqlalchemy as sa

from alembic import op
from app.db.seed import PLAN_MODEL_ROWS, PLAN_ROWS

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

plan = sa.table(
    "plan",
    sa.column("code", sa.String),
    sa.column("requests_per_minute", sa.Integer),
    sa.column("max_concurrency", sa.Integer),
)
plan_model = sa.table(
    "plan_model",
    sa.column("plan_code", sa.String),
    sa.column("model_name", sa.String),
)


def upgrade() -> None:
    op.bulk_insert(plan, PLAN_ROWS, multiinsert=False)
    op.bulk_insert(plan_model, PLAN_MODEL_ROWS, multiinsert=False)


def downgrade() -> None:
    codes = [row["code"] for row in PLAN_ROWS]
    op.execute(plan_model.delete().where(plan_model.c.plan_code.in_(codes)))
    op.execute(plan.delete().where(plan.c.code.in_(codes)))
