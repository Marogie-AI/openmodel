import pytest
from httpx import AsyncClient

from app.auth.keys import hash_secret
from tests.conftest import ADMIN_HEADERS

pytestmark = pytest.mark.db


async def test_create_list_and_revoke_own_keys(auth_client: AsyncClient) -> None:
    created = await auth_client.post("/api/keys")
    assert created.status_code == 201, created.text
    body = created.json()
    assert set(body) == {"id", "key", "prefix", "created_at"}
    assert body["key"].startswith(f"{body['prefix']}_")

    listed = await auth_client.get("/api/keys")
    assert listed.status_code == 200
    data = listed.json()["data"]
    assert listed.json()["object"] == "list"
    assert len(data) == 2
    assert set(data[0]) == {"id", "prefix", "created_at", "revoked_at"}
    assert all(key["revoked_at"] is None for key in data)

    assert (await auth_client.delete(f"/api/keys/{body['id']}")).status_code == 204

    revoked = next(
        key
        for key in (await auth_client.get("/api/keys")).json()["data"]
        if key["id"] == body["id"]
    )
    assert revoked["revoked_at"] is not None


async def test_the_new_key_authenticates(auth_client: AsyncClient) -> None:
    created = await auth_client.post("/api/keys")

    response = await auth_client.get(
        "/v1/models", headers={"Authorization": f"Bearer {created.json()['key']}"}
    )

    assert response.status_code == 200


async def test_revoked_key_no_longer_authenticates(auth_client: AsyncClient) -> None:
    created = (await auth_client.post("/api/keys")).json()
    headers = {"Authorization": f"Bearer {created['key']}"}
    assert (await auth_client.get("/v1/models", headers=headers)).status_code == 200

    assert (await auth_client.delete(f"/api/keys/{created['id']}")).status_code == 204

    assert (await auth_client.get("/v1/models", headers=headers)).status_code == 401


async def test_another_users_key_is_not_visible_or_deletable(
    auth_client: AsyncClient, db_client: AsyncClient
) -> None:
    org = await db_client.post(
        "/admin/orgs", json={"name": "other", "plan_code": "free"}, headers=ADMIN_HEADERS
    )
    user = await db_client.post(
        "/admin/users",
        json={"organization_id": org.json()["id"], "email": "other@example.com"},
        headers=ADMIN_HEADERS,
    )
    other = await db_client.post(f"/admin/users/{user.json()['id']}/keys", headers=ADMIN_HEADERS)
    other_id = other.json()["id"]

    listed = await auth_client.get("/api/keys")
    assert other_id not in [key["id"] for key in listed.json()["data"]]

    response = await auth_client.delete(f"/api/keys/{other_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_create_key_hashes_off_the_event_loop(
    auth_client: AsyncClient, record_to_thread: list[object]
) -> None:
    assert (await auth_client.post("/api/keys")).status_code == 201

    assert hash_secret in record_to_thread
