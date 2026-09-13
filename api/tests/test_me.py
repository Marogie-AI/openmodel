import pytest
from httpx import AsyncClient

from tests.conftest import TEST_PRINCIPAL


async def test_me_returns_the_principals_non_secret_fields(client: AsyncClient) -> None:
    response = await client.get("/api/me")

    assert response.status_code == 200
    assert response.json() == {
        "organization_id": str(TEST_PRINCIPAL.organization_id),
        "user_id": str(TEST_PRINCIPAL.user_id),
        "plan_code": "free",
        "requests_per_minute": 60,
        "max_concurrency": 2,
        "models": ["nomic-embed-text", "qwen2.5:0.5b"],
    }


@pytest.mark.db
async def test_me_rejects_a_request_without_a_key(db_client: AsyncClient) -> None:
    response = await db_client.get("/api/me")

    assert response.status_code == 401
