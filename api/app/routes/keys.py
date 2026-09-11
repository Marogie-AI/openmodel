"""Self-service API keys: the caller manages the keys of their own user, nobody else's."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.keys import generate_key, hash_secret_async
from app.auth.principal import Principal, drop_cached, require_principal
from app.config import settings
from app.db.models import ApiKey
from app.db.session import get_session
from app.errors import NotFound
from app.schemas.admin import KeyOut
from app.schemas.keys import KeyInfo, KeyList

router = APIRouter(prefix="/api/keys")

Session = Annotated[AsyncSession, Depends(get_session)]
Caller = Annotated[Principal, Depends(require_principal)]


@router.post("", status_code=201)
async def create_key(principal: Caller, session: Session) -> KeyOut:
    generated = generate_key(settings.environment)
    key = ApiKey(
        user_id=principal.user_id,
        key_id=generated.key_id,
        secret_hash=await hash_secret_async(generated.secret, settings.key_pepper),
        prefix=generated.prefix,
    )
    session.add(key)
    await session.flush()
    await session.refresh(key)
    await session.commit()
    return KeyOut(id=key.id, key=generated.raw, prefix=key.prefix, created_at=key.created_at)


@router.get("")
async def list_keys(principal: Caller, session: Session) -> KeyList:
    keys = await session.scalars(
        select(ApiKey).where(ApiKey.user_id == principal.user_id).order_by(ApiKey.created_at)
    )
    return KeyList(
        data=[
            KeyInfo(
                id=key.id, prefix=key.prefix, created_at=key.created_at, revoked_at=key.revoked_at
            )
            for key in keys
        ]
    )


@router.delete("/{key_id}", status_code=204)
async def revoke_key(key_id: UUID, principal: Caller, session: Session, request: Request) -> None:
    key = await session.get(ApiKey, key_id)
    # Someone else's key is indistinguishable from a missing one.
    if key is None or key.user_id != principal.user_id:
        raise NotFound(f"The key '{key_id}' does not exist.")
    key.revoked_at = datetime.now(UTC)
    # Commit before dropping the cache: dropping first lets a concurrent request re-cache the key
    # from the not-yet-revoked row, and keep it usable for the whole TTL.
    await session.commit()
    await drop_cached(request.app.state.redis, key.key_id)
