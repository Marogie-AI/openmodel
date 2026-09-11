"""Acceptance tests against a running compose stack, driven by the OpenAI SDK.

The stack ships with no keys, so the module bootstraps its own organization, user and key
through the admin API before anything else runs.
"""

import os
import time
from uuid import uuid4

import httpx
import pytest
from openai import OpenAI

pytestmark = pytest.mark.e2e

BASE_URL = os.environ.get("OPENMODEL_BASE_URL", "http://localhost:8000/v1")
ORIGIN = BASE_URL.removesuffix("/v1")
ADMIN_HEADERS = {
    "X-Admin-Token": os.environ.get("OPENMODEL_ADMIN_TOKEN", "dev-admin-token-change-me")
}
# The cluster serves a self-signed cert for api.openmodel.test. Set
# OPENMODEL_INSECURE_TLS=1 to trust it; unset, verification stays on.
VERIFY = os.environ.get("OPENMODEL_INSECURE_TLS") != "1"
HTTP = httpx.Client(verify=VERIFY)

CHAT_MODEL = "qwen2.5:0.5b"


def _created(path: str, body: dict[str, object]) -> dict[str, object]:
    response = HTTP.post(f"{ORIGIN}{path}", headers=ADMIN_HEADERS, json=body, timeout=30)
    response.raise_for_status()
    data: dict[str, object] = response.json()
    return data


@pytest.fixture(scope="module")
def api_key() -> str:
    """A fresh key on the `pro` plan, made the way an operator would make one."""
    tag = uuid4().hex[:8]
    org = _created("/admin/orgs", {"name": f"e2e-{tag}", "plan_code": "pro"})
    user = _created(
        "/admin/users", {"organization_id": org["id"], "email": f"e2e-{tag}@example.test"}
    )
    key = HTTP.post(f"{ORIGIN}/admin/users/{user['id']}/keys", headers=ADMIN_HEADERS, timeout=30)
    key.raise_for_status()
    raw: str = key.json()["key"]
    return raw


@pytest.fixture(scope="module")
def client(api_key: str) -> OpenAI:
    if VERIFY:
        return OpenAI(base_url=BASE_URL, api_key=api_key)
    return OpenAI(base_url=BASE_URL, api_key=api_key, http_client=httpx.Client(verify=False))


def test_a_request_without_a_key_is_rejected() -> None:
    response = HTTP.get(f"{BASE_URL}/models", timeout=30)

    assert response.status_code == 401
    assert response.json() == {
        "error": {
            "message": "Missing or invalid API key",
            "type": "invalid_request_error",
            "code": "invalid_api_key",
        }
    }


def test_models_list_contains_chat_model(client: OpenAI) -> None:
    assert CHAT_MODEL in [m.id for m in client.models.list()]


def test_models_list_contains_the_pro_only_model(client: OpenAI) -> None:
    assert "qwen2.5-coder:0.5b" in [m.id for m in client.models.list()]


def test_chat_completion(client: OpenAI) -> None:
    response = client.chat.completions.create(
        model=CHAT_MODEL,
        messages=[{"role": "user", "content": "Say hi."}],
        max_tokens=64,
    )
    assert response.choices[0].message.content
    assert response.usage is not None
    assert response.usage.total_tokens > 0


def test_chat_completion_stream(client: OpenAI) -> None:
    chunks = list(
        client.chat.completions.create(
            model=CHAT_MODEL,
            messages=[{"role": "user", "content": "Say hi."}],
            max_tokens=64,
            stream=True,
        )
    )
    assert len(chunks) >= 2
    assert chunks[-1].choices[0].finish_reason == "stop"


def test_text_completion(client: OpenAI) -> None:
    response = client.completions.create(
        model=CHAT_MODEL,
        prompt="The capital of France is",
        max_tokens=64,
    )
    assert response.choices[0].text


def test_embedding(client: OpenAI) -> None:
    response = client.embeddings.create(model="nomic-embed-text", input="hello")
    assert len(response.data[0].embedding) == 768


def test_a_served_request_carries_the_plan_rate_limit(api_key: str) -> None:
    response = HTTP.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": CHAT_MODEL,
            "messages": [{"role": "user", "content": "Hi."}],
            "max_tokens": 8,
        },
        timeout=120,
    )

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "600"


def test_usage_counts_the_calls(api_key: str) -> None:
    headers = {"Authorization": f"Bearer {api_key}"}
    # Metering is a background task, so the last call's row can land after its response.
    deadline = time.monotonic() + 2
    while True:
        usage = HTTP.get(f"{ORIGIN}/api/usage", headers=headers, timeout=30).json()
        if usage["requests"] >= 4 or time.monotonic() > deadline:
            break
        time.sleep(0.1)

    assert usage["requests"] >= 4
    assert usage["prompt_tokens"] > 0
