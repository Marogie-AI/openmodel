from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/v1/models")
async def list_models(request: Request) -> dict[str, Any]:
    registry = request.app.state.registry
    return {
        "object": "list",
        "data": [
            {"id": model.name, "object": "model", "created": 0, "owned_by": "openmodel"}
            for model in registry.list()
        ],
    }
