"""Who the caller is: the console's cheap key-validation call."""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth.principal import Principal, require_principal
from app.schemas.me import Me

router = APIRouter(prefix="/api/me")


@router.get("")
async def read_me(principal: Annotated[Principal, Depends(require_principal)]) -> Me:
    return Me(
        organization_id=principal.organization_id,
        user_id=principal.user_id,
        plan_code=principal.plan_code,
        requests_per_minute=principal.requests_per_minute,
        max_concurrency=principal.max_concurrency,
        models=sorted(principal.models),
    )
