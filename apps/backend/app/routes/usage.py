"""Usage reporting for the caller's own organization."""

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.principal import Principal, require_principal
from app.db.session import get_session
from app.schemas.usage import UsageGroup, UsageSummary
from app.usage import GroupBy, aggregate

router = APIRouter(prefix="/api/usage")

Session = Annotated[AsyncSession, Depends(get_session)]
Caller = Annotated[Principal, Depends(require_principal)]

DEFAULT_WINDOW = timedelta(days=30)


@router.get("")
async def read_usage(
    principal: Caller,
    session: Session,
    since: Annotated[datetime | None, Query(alias="from")] = None,
    until: Annotated[datetime | None, Query(alias="to")] = None,
    group_by: Annotated[GroupBy | None, Query()] = None,
) -> UsageSummary:
    now = datetime.now(UTC)
    since = since or now - DEFAULT_WINDOW
    until = until or now
    totals = (await aggregate(session, principal.organization_id, since, until, None))[0]
    groups = (
        await aggregate(session, principal.organization_id, since, until, group_by)
        if group_by
        else []
    )
    return UsageSummary(
        organization_id=principal.organization_id,
        since=since,
        until=until,
        requests=totals["requests"],
        prompt_tokens=totals["prompt_tokens"],
        completion_tokens=totals["completion_tokens"],
        groups=[UsageGroup.model_validate(group) for group in groups],
    )
