from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.auth.principal import Principal, require_principal

router = APIRouter()


@router.get("/v1/models")
async def list_models(
    request: Request, principal: Annotated[Principal, Depends(require_principal)]
) -> dict[str, Any]:
    registry = request.app.state.registry
    names = sorted({model.name for model in registry.list()} & principal.models)
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": 0, "owned_by": "openmodel"} for name in names
        ],
    }
