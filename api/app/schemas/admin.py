from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class PlanOut(BaseModel):
    code: str
    requests_per_minute: int
    max_concurrency: int
    models: list[str]


class PlanUpsert(BaseModel):
    requests_per_minute: int = Field(gt=0)
    max_concurrency: int = Field(gt=0)
    models: list[str]


class OrgCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    plan_code: str = Field(min_length=1, max_length=32)


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
