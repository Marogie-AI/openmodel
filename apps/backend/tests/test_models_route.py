from httpx import AsyncClient


async def test_list_models_returns_openai_envelope(client: AsyncClient) -> None:
    response = await client.get("/v1/models")

    assert response.status_code == 200
    assert response.json() == {
        "object": "list",
        "data": [
            {"id": "nomic-embed-text", "object": "model", "created": 0, "owned_by": "openmodel"},
            {"id": "qwen2.5:0.5b", "object": "model", "created": 0, "owned_by": "openmodel"},
        ],
    }
