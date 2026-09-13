from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from app.errors import CapabilityMismatch, UnknownModel

Capability = Literal["chat", "completion", "embedding"]


class ModelSpec(BaseModel):
    name: str
    backend_url: str
    capabilities: list[Capability]


class Registry:
    """Static model catalogue: which backend serves a model, and for what."""

    def __init__(self, models: list[ModelSpec]) -> None:
        self._models = {model.name: model for model in models}

    @classmethod
    def from_yaml(cls, path: Path) -> "Registry":
        raw = yaml.safe_load(path.read_text()) or {}
        return cls([ModelSpec.model_validate(entry) for entry in raw.get("models", [])])

    def list(self) -> list[ModelSpec]:
        return sorted(self._models.values(), key=lambda model: model.name)

    def get(self, name: str) -> ModelSpec:
        try:
            return self._models[name]
        except KeyError:
            raise UnknownModel(name) from None

    def require(self, name: str, capability: Capability) -> ModelSpec:
        model = self.get(name)
        if capability not in model.capabilities:
            raise CapabilityMismatch(name, capability)
        return model

    def backend_urls(self) -> set[str]:
        return {model.backend_url for model in self._models.values()}
