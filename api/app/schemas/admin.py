from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field


class PlanOut(BaseModel):
    code: str
    requests_per_minute: int
    max_concurrency: int
    models: list[str]


class PlanUpsert(BaseModel):
    # Upper bounds as well as lower: anything past an int32 column is a 422, not a 500 from the
    # driver. The ceilings are far above any real plan.
    requests_per_minute: int = Field(ge=1, le=1_000_000)
    max_concurrency: int = Field(ge=1, le=1_000_000)
    models: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(max_length=100)


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    plan_code: str = Field(min_length=1, max_length=32, pattern=r"^[a-z0-9_-]+$")


class OrgOut(BaseModel):
    id: UUID
    name: str
    plan_code: str
    created_at: datetime


class UserCreate(BaseModel):
    organization_id: UUID
    email: str = Field(min_length=3, max_length=320)


class UserOut(BaseModel):
    id: UUID
    organization_id: UUID
    email: str
    created_at: datetime


class KeyOut(BaseModel):
    id: UUID
    key: str
    prefix: str
    created_at: datetime
