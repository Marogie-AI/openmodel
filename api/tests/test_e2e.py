"""Acceptance tests against a running compose stack, driven by the OpenAI SDK."""

import os

import pytest
from openai import OpenAI

pytestmark = pytest.mark.e2e


@pytest.fixture(scope="module")
def client() -> OpenAI:
    base_url = os.environ.get("OPENMODEL_BASE_URL", "http://localhost:8000/v1")
    return OpenAI(base_url=base_url, api_key="unused")


def test_models_list_contains_chat_model(client: OpenAI) -> None:
    assert "qwen2.5:0.5b" in [m.id for m in client.models.list()]


def test_chat_completion(client: OpenAI) -> None:
    response = client.chat.completions.create(
        model="qwen2.5:0.5b",
        messages=[{"role": "user", "content": "Say hi."}],
        max_tokens=64,
    )
    assert response.choices[0].message.content
    assert response.usage is not None
    assert response.usage.total_tokens > 0


def test_chat_completion_stream(client: OpenAI) -> None:
    chunks = list(
        client.chat.completions.create(
            model="qwen2.5:0.5b",
            messages=[{"role": "user", "content": "Say hi."}],
            max_tokens=64,
            stream=True,
        )
    )
    assert len(chunks) >= 2
    assert chunks[-1].choices[0].finish_reason == "stop"


def test_text_completion(client: OpenAI) -> None:
    response = client.completions.create(
        model="qwen2.5:0.5b",
        prompt="The capital of France is",
        max_tokens=64,
    )
    assert response.choices[0].text


def test_embedding(client: OpenAI) -> None:
    response = client.embeddings.create(model="nomic-embed-text", input="hello")
    assert len(response.data[0].embedding) == 768
