"""Per-request usage metering: a background writer and the aggregation behind /api/usage."""

import asyncio
import time
from dataclasses import asdict, dataclass
from uuid import UUID

import structlog
from fastapi import Request as HttpRequest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import Request
from app.schemas.backend import Usage

log = structlog.get_logger()


@dataclass(frozen=True)
class UsageRecord:
    api_key_id: UUID
    model_name: str
    endpoint: str
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    status_code: int


def record_for(
    api_key_id: UUID,
    model: str,
    endpoint: str,
    start: float,
    status_code: int,
    usage: Usage | None = None,
) -> UsageRecord:
    """One record for a finished call; `start` is the perf counter taken at route entry."""
    return UsageRecord(
        api_key_id=api_key_id,
        model_name=model,
        endpoint=endpoint,
        prompt_tokens=usage.prompt_tokens if usage else 0,
        completion_tokens=usage.completion_tokens if usage else 0,
        latency_ms=int((time.perf_counter() - start) * 1000),
        status_code=status_code,
    )


async def write(sessionmaker: async_sessionmaker[AsyncSession], record: UsageRecord) -> None:
    """Insert one usage row. Metering must never fail a request, so nothing escapes here."""
    try:
        async with sessionmaker() as session:
            session.add(Request(**asdict(record)))
            await session.commit()
    except Exception as exc:
        log.warning("usage_write_failed", exc_info=exc)


def schedule_write(
    sessionmaker: async_sessionmaker[AsyncSession],
    tasks: set[asyncio.Task[None]],
    record: UsageRecord,
) -> None:
    """The real sink: a background task, held in `tasks` so it is not garbage collected."""
    task = asyncio.create_task(write(sessionmaker, record))
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def schedule(request: HttpRequest, record: UsageRecord) -> None:
    """Hand one record to the app's sink (the background writer, or a test double)."""
    request.app.state.usage_sink(record)
