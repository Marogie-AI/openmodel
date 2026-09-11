from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class UsageGroup(BaseModel):
    key: str | None
    requests: int
    prompt_tokens: int
    completion_tokens: int


class UsageSummary(BaseModel):
    # "from" is a keyword, so the field is `since` and the alias carries the wire name.
    model_config = ConfigDict(populate_by_name=True)

    organization_id: UUID
    since: datetime = Field(alias="from")
    until: datetime = Field(alias="to")
    requests: int
    prompt_tokens: int
    completion_tokens: int
    groups: list[UsageGroup]
