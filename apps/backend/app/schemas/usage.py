from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class UsageGroup(BaseModel):
    key: str | None
    requests: int
    prompt_tokens: int
    completion_tokens: int


class UsageSummary(BaseModel):
    # "from" is a keyword, so the field is `since`; the serialization alias carries
    # the wire name and the constructor keeps the Python name.

    organization_id: UUID
    since: datetime = Field(serialization_alias="from")
    until: datetime = Field(serialization_alias="to")
    requests: int
    prompt_tokens: int
    completion_tokens: int
    groups: list[UsageGroup]
