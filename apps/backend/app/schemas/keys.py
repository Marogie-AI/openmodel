from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class KeyInfo(BaseModel):
    id: UUID
    prefix: str
    created_at: datetime
    revoked_at: datetime | None


class KeyList(BaseModel):
    object: Literal["list"] = "list"
    data: list[KeyInfo]
