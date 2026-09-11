import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.keys import parse_key, verify_secret
from app.config import settings
from app.db.models import ApiKey
from tests.conftest import ADMIN_HEADERS

pytestmark = pytest.mark.db


async def make_org(client: AsyncClient, name: str = "acme", plan: str = "free") -> str:
    response = await client.post(
        "/admin/orgs", json={"name": name, "plan_code": plan}, headers=ADMIN_HEADERS
    )
    assert response.status_code == 201, response.text
    org_id: str = response.json()["id"]
    return org_id


async def make_user(client: AsyncClient, org_id: str, email: str = "a@example.com") -> str:
    response = await client.post(
        "/admin/users",
        json={"organization_id": org_id, "email": email},
        headers=ADMIN_HEADERS,
    )
    assert response.status_code == 201, response.text
    user_id: str = response.json()["id"]
    return user_id


@pytest.mark.parametrize("headers", [{}, {"X-Admin-Token": "wrong"}])
async def test_admin_requires_the_admin_token(
    db_client: AsyncClient, headers: dict[str, str]
) -> None:
    response = await db_client.get("/admin/plans", headers=headers)

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {
        "error": {
            "message": "Invalid admin token",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }
    }


async def test_list_plans_shows_the_seeded_plans(db_client: AsyncClient) -> None:
    response = await db_client.get("/admin/plans", headers=ADMIN_HEADERS)

    assert response.status_code == 200
    plans = {plan["code"]: plan for plan in response.json()}
    assert plans["free"]["requests_per_minute"] == 60
    assert plans["free"]["max_concurrency"] == 2
    assert plans["free"]["models"] == ["nomic-embed-text", "qwen2.5:0.5b"]


async def test_upsert_plan_replaces_limits_and_models(db_client: AsyncClient) -> None:
    response = await db_client.put(
        "/admin/plans/free",
        json={"requests_per_minute": 5, "max_concurrency": 1, "models": ["qwen2.5:0.5b"]},
        headers=ADMIN_HEADERS,
    )

    assert response.status_code == 200
    assert response.json() == {
        "code": "free",
        "requests_per_minute": 5,
        "max_concurrency": 1,
        "models": ["qwen2.5:0.5b"],
    }

    listed = await db_client.get("/admin/plans", headers=ADMIN_HEADERS)
    free = next(plan for plan in listed.json() if plan["code"] == "free")
    assert free == response.json()


async def test_create_org_and_list(db_client: AsyncClient) -> None:
    org_id = await make_org(db_client)

    listed = await db_client.get("/admin/orgs", headers=ADMIN_HEADERS)
    assert [org["id"] for org in listed.json()] == [org_id]
    assert listed.json()[0]["name"] == "acme"
    assert listed.json()[0]["plan_code"] == "free"
    assert listed.json()[0]["created_at"]


async def test_create_org_rejects_an_unknown_plan_and_a_duplicate_name(
    db_client: AsyncClient,
) -> None:
    unknown = await db_client.post(
        "/admin/orgs", json={"name": "acme", "plan_code": "ghost"}, headers=ADMIN_HEADERS
    )
    assert unknown.status_code == 404
    assert unknown.json()["error"]["code"] == "not_found"

    await make_org(db_client)
    duplicate = await db_client.post(
        "/admin/orgs", json={"name": "acme", "plan_code": "free"}, headers=ADMIN_HEADERS
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"


async def test_create_user_rejects_an_unknown_org_and_a_duplicate_email(
    db_client: AsyncClient,
) -> None:
    unknown = await db_client.post(
        "/admin/users",
        json={"organization_id": "00000000-0000-0000-0000-000000000000", "email": "a@example.com"},
        headers=ADMIN_HEADERS,
    )
    assert unknown.status_code == 404

    org_id = await make_org(db_client)
    user_id = await make_user(db_client, org_id)
    assert user_id

    duplicate = await db_client.post(
        "/admin/users",
        json={"organization_id": org_id, "email": "a@example.com"},
        headers=ADMIN_HEADERS,
    )
    assert duplicate.status_code == 409


async def test_create_key_returns_the_raw_key_once_and_stores_only_a_hash(
    db_client: AsyncClient, session: AsyncSession
) -> None:
    org_id = await make_org(db_client)
    user_id = await make_user(db_client, org_id)

    response = await db_client.post(f"/admin/users/{user_id}/keys", headers=ADMIN_HEADERS)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"id", "key", "prefix", "created_at"}
    parsed = parse_key(body["key"])
    assert parsed is not None
    key_id, secret = parsed
    assert body["prefix"] == f"om_{settings.environment}_{key_id}"

    row = (await session.scalars(select(ApiKey))).one()
    assert str(row.id) == body["id"]
    assert row.key_id == key_id
    assert row.prefix == body["prefix"]
    assert row.revoked_at is None
    assert row.secret_hash.startswith("$argon2id$")
    assert secret not in row.secret_hash
    assert verify_secret(row.secret_hash, secret, settings.key_pepper)


async def test_create_key_rejects_an_unknown_user(db_client: AsyncClient) -> None:
    response = await db_client.post(
        "/admin/users/00000000-0000-0000-0000-000000000000/keys", headers=ADMIN_HEADERS
    )

    assert response.status_code == 404
