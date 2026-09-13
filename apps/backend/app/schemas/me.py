from uuid import UUID

from pydantic import BaseModel


class Me(BaseModel):
    """The caller's own identity and limits: non-secret principal fields only."""

    organization_id: UUID
    user_id: UUID
    plan_code: str
    requests_per_minute: int
    max_concurrency: int
    models: list[str]
