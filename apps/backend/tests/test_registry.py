from pathlib import Path

import pytest

from app.errors import CapabilityMismatch, UnknownModel
from app.router import ModelSpec, Registry

YAML = """
models:
  - name: zeta
    backend_url: http://a:11434
    capabilities: [chat, completion]
  - name: alpha
    backend_url: http://b:11434
    capabilities: [embedding]
"""


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    path = tmp_path / "models.yaml"
    path.write_text(YAML)
    return Registry.from_yaml(path)


def test_from_yaml_parses_models(registry: Registry) -> None:
    assert registry.get("zeta") == ModelSpec(
        name="zeta", backend_url="http://a:11434", capabilities=["chat", "completion"]
    )


def test_list_is_sorted_by_name(registry: Registry) -> None:
    assert [model.name for model in registry.list()] == ["alpha", "zeta"]


def test_get_unknown_raises(registry: Registry) -> None:
    with pytest.raises(UnknownModel) as exc_info:
        registry.get("missing")

    assert exc_info.value.status_code == 404
    assert exc_info.value.code == "model_not_found"


def test_require_returns_model_with_capability(registry: Registry) -> None:
    assert registry.require("zeta", "chat").name == "zeta"


def test_require_mismatched_capability_raises(registry: Registry) -> None:
    with pytest.raises(CapabilityMismatch) as exc_info:
        registry.require("alpha", "chat")

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "model_capability"


def test_backend_urls_are_deduplicated() -> None:
    registry = Registry(
        [
            ModelSpec(name="a", backend_url="http://a:11434", capabilities=["chat"]),
            ModelSpec(name="b", backend_url="http://a:11434", capabilities=["chat"]),
        ]
    )

    assert registry.backend_urls() == {"http://a:11434"}
