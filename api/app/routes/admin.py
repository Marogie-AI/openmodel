"""Admin CRUD for plans, organizations, users, and API keys. Gated by the admin token."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Path
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.admin import require_admin
from app.auth.keys import generate_key, hash_secret
from app.config import settings
from app.db.models import ApiKey, AppUser, Organization, Plan, PlanModel
from app.db.session import get_session
from app.errors import Conflict, NotFound
from app.schemas.admin import KeyOut, OrgCreate, OrgOut, PlanOut, PlanUpsert, UserCreate, UserOut

router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])

Session = Annotated[AsyncSession, Depends(get_session)]


async def _flush(session: AsyncSession, message: str) -> None:
    """Flush pending inserts, turning a losing unique-constraint race into a 409."""
    try:
        await session.flush()
    except IntegrityError as exc:
        raise Conflict(message) from exc


async def _plan_models(session: AsyncSession) -> dict[str, list[str]]:
    rows = await session.execute(select(PlanModel.plan_code, PlanModel.model_name))
    models: dict[str, list[str]] = {}
    for plan_code, model_name in rows:
        models.setdefault(plan_code, []).append(model_name)
    return models


@router.get("/plans")
async def list_plans(session: Session) -> list[PlanOut]:
    models = await _plan_models(session)
    plans = (await session.scalars(select(Plan).order_by(Plan.code))).all()
    return [
        PlanOut(
            code=plan.code,
            requests_per_minute=plan.requests_per_minute,
            max_concurrency=plan.max_concurrency,
            models=sorted(models.get(plan.code, [])),
        )
        for plan in plans
    ]


@router.put("/plans/{code}")
async def upsert_plan(
    code: Annotated[str, Path(min_length=1, max_length=32)], body: PlanUpsert, session: Session
) -> PlanOut:
    plan = await session.get(Plan, code)
    if plan is None:
        plan = Plan(code=code)
        session.add(plan)
    plan.requests_per_minute = body.requests_per_minute
    plan.max_concurrency = body.max_concurrency
    await _flush(session, f"Concurrent update to plan '{code}'; retry.")

    await session.execute(delete(PlanModel).where(PlanModel.plan_code == code))
    session.add_all(PlanModel(plan_code=code, model_name=name) for name in sorted(set(body.models)))
    await session.flush()
    return PlanOut(
        code=code,
        requests_per_minute=plan.requests_per_minute,
        max_concurrency=plan.max_concurrency,
        models=sorted(set(body.models)),
    )


@router.post("/orgs", status_code=201)
async def create_org(body: OrgCreate, session: Session) -> OrgOut:
    if await session.get(Plan, body.plan_code) is None:
        raise NotFound(f"The plan '{body.plan_code}' does not exist.")
    if await session.scalar(select(Organization.id).where(Organization.name == body.name)):
        raise Conflict(f"An organization named '{body.name}' already exists.")

    org = Organization(name=body.name, plan_code=body.plan_code)
    session.add(org)
    await _flush(session, f"An organization named '{body.name}' already exists.")
    await session.refresh(org)
    return OrgOut(id=org.id, name=org.name, plan_code=org.plan_code, created_at=org.created_at)


@router.get("/orgs")
async def list_orgs(session: Session) -> list[OrgOut]:
    orgs = (await session.scalars(select(Organization).order_by(Organization.created_at))).all()
    return [
        OrgOut(id=org.id, name=org.name, plan_code=org.plan_code, created_at=org.created_at)
        for org in orgs
    ]


@router.post("/users", status_code=201)
async def create_user(body: UserCreate, session: Session) -> UserOut:
    if await session.get(Organization, body.organization_id) is None:
        raise NotFound(f"The organization '{body.organization_id}' does not exist.")
    if await session.scalar(select(AppUser.id).where(AppUser.email == body.email)):
        raise Conflict(f"A user with email '{body.email}' already exists.")

    user = AppUser(organization_id=body.organization_id, email=body.email)
    session.add(user)
    await _flush(session, f"A user with email '{body.email}' already exists.")
    await session.refresh(user)
    return UserOut(
        id=user.id,
        organization_id=user.organization_id,
        email=user.email,
        created_at=user.created_at,
    )


@router.post("/users/{user_id}/keys", status_code=201)
async def create_key(user_id: UUID, session: Session) -> KeyOut:
    if await session.get(AppUser, user_id) is None:
        raise NotFound(f"The user '{user_id}' does not exist.")

    generated = generate_key(settings.environment)
    key = ApiKey(
        user_id=user_id,
        key_id=generated.key_id,
        secret_hash=hash_secret(generated.secret, settings.key_pepper),
        prefix=generated.prefix,
    )
    session.add(key)
    await _flush(session, "That key already exists.")
    await session.refresh(key)
    return KeyOut(id=key.id, key=generated.raw, prefix=key.prefix, created_at=key.created_at)
