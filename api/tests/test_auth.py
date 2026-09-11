import pytest
import redis.asyncio as aioredis
from httpx import AsyncClient

from app.auth import principal as principal_module
from app.auth.keys import generate_key, parse_key
from tests.conftest import ADMIN_HEADERS

pytestmark = pytest.mark.db

UNAUTHORIZED = {
    "error": {
        "message": "Missing or invalid API key",
        "type": "invalid_request_error",
        "code": "invalid_api_key",
    }
}

FREE_MODELS = ["nomic-embed-text", "qwen2.5:0.5b"]


def key_id_of(raw_key: str) -> str:
    parsed = parse_key(raw_key)
    assert parsed is not None
    return parsed[0]


async def test_missing_header_is_a_401_envelope(db_client: AsyncClient) -> None:
    response = await db_client.get("/v1/models")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == UNAUTHORIZED


@pytest.mark.parametrize(
    "header",
    ["", "notbearer om_dev_x", "Bearer", "Bearer ", "Bearer not-a-key", "om_dev_abc"],
)
async def test_malformed_authorization_is_rejected(db_client: AsyncClient, header: str) -> None:
    response = await db_client.get("/v1/models", headers={"Authorization": header})

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


async def test_unknown_key_id_is_rejected(db_client: AsyncClient) -> None:
    response = await db_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {generate_key('dev').raw}"}
    )

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


async def test_wrong_secret_is_rejected(db_client: AsyncClient, principal_key: str) -> None:
    wrong = f"{principal_key[: -len('x' * 32)]}{'a' * 32}"

    response = await db_client.get("/v1/models", headers={"Authorization": f"Bearer {wrong}"})

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


async def test_valid_key_lists_the_plan_models(auth_client: AsyncClient) -> None:
    response = await auth_client.get("/v1/models")

    assert response.status_code == 200
    assert [model["id"] for model in response.json()["data"]] == FREE_MODELS


async def test_second_call_is_served_from_the_cache(
    auth_client: AsyncClient,
    principal_key: str,
    redis_client: aioredis.Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert (await auth_client.get("/v1/models")).status_code == 200
    assert await redis_client.exists(f"auth:{key_id_of(principal_key)}")

    def boom(*args: object, **kwargs: object) -> bool:
        raise AssertionError("argon2 verify ran on a cache hit")

    monkeypatch.setattr(principal_module, "verify_secret", boom)

    assert (await auth_client.get("/v1/models")).status_code == 200


async def test_cached_key_id_with_a_wrong_secret_is_rejected(
    auth_client: AsyncClient, principal_key: str
) -> None:
    """Cache poisoning: the right key_id must not authorize a request carrying a bad secret."""
    assert (await auth_client.get("/v1/models")).status_code == 200
    wrong = f"{principal_key[: -len('x' * 32)]}{'b' * 32}"

    response = await auth_client.get("/v1/models", headers={"Authorization": f"Bearer {wrong}"})

    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


async def test_revoked_key_is_rejected_and_dropped_from_the_cache(
    auth_client: AsyncClient, principal_key: str, redis_client: aioredis.Redis
) -> None:
    listed = await auth_client.get("/api/keys")
    key_id = listed.json()["data"][0]["id"]
    assert (await auth_client.delete(f"/api/keys/{key_id}")).status_code == 204

    assert not await redis_client.exists(f"auth:{key_id_of(principal_key)}")
    response = await auth_client.get("/v1/models")
    assert response.status_code == 401
    assert response.json() == UNAUTHORIZED


async def test_a_keys_plan_models_come_from_its_own_plan(db_client: AsyncClient) -> None:
    org = await db_client.post(
        "/admin/orgs", json={"name": "pro-co", "plan_code": "pro"}, headers=ADMIN_HEADERS
    )
    user = await db_client.post(
        "/admin/users",
        json={"organization_id": org.json()["id"], "email": "pro@example.com"},
        headers=ADMIN_HEADERS,
    )
    key = await db_client.post(f"/admin/users/{user.json()['id']}/keys", headers=ADMIN_HEADERS)

    response = await db_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {key.json()['key']}"}
    )

    # The pro plan also grants qwen2.5-coder:0.5b, which the test registry does not serve.
    assert [model["id"] for model in response.json()["data"]] == FREE_MODELS
