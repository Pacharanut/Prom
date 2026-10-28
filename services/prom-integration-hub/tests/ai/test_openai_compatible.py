"""OpenAICompatibleLlmClient against httpx.MockTransport — request shape, JSON extraction,
the response_format fallback, and error mapping (incl. the NT AI Hub "web page, not API" case
found during setup)."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from src.adapters.ai.openai_compatible import OpenAICompatibleLlmClient
from src.application.ai.ports import AiConnection
from src.shared.errors import AiCredentialInvalidError, AiResponseInvalidError, AiUnavailableError

_CONNECTION = AiConnection(
    base_url="https://aihub.example/core-api/v1",
    api_key="secret-key",
    model="gpt-oss-120b",
    timeout_seconds=5,
)


def _completion(content: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


async def _call(handler: Any) -> dict[str, Any]:
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await OpenAICompatibleLlmClient(client=client).complete_json(
            _CONNECTION,
            system_prompt="sys",
            user_message="งานของฉันวันนี้",
            schema_name="prom_command",
            schema={"type": "object"},
        )


@pytest.mark.asyncio
async def test_sends_bearer_key_model_and_json_schema() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_completion('{"intent": "list_my_tasks"}'))

    assert await _call(handler) == {"intent": "list_my_tasks"}
    request = seen[0]
    assert str(request.url) == "https://aihub.example/core-api/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer secret-key"
    body = json.loads(request.content)
    assert body["model"] == "gpt-oss-120b"
    assert body["temperature"] == 0
    assert body["response_format"]["type"] == "json_schema"
    assert body["messages"][1] == {"role": "user", "content": "งานของฉันวันนี้"}


@pytest.mark.asyncio
async def test_extracts_json_from_think_block_and_fences() -> None:
    content = '<think>{"intent": "wrong"}</think>\n```json\n{"intent": "search"}\n```'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion(content))

    assert await _call(handler) == {"intent": "search"}


@pytest.mark.asyncio
async def test_retries_without_response_format_when_server_rejects_it() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        if "response_format" in body:
            return httpx.Response(400, text="unsupported parameter: response_format")
        return httpx.Response(200, json=_completion('{"intent": "show_menu"}'))

    assert await _call(handler) == {"intent": "show_menu"}
    assert len(bodies) == 2 and "response_format" not in bodies[1]


@pytest.mark.asyncio
async def test_web_page_instead_of_api_is_unavailable_with_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="<!DOCTYPE html>", headers={"content-type": "text/html"})

    with pytest.raises(AiUnavailableError, match="core-api/v1"):
        await _call(handler)


@pytest.mark.asyncio
async def test_401_is_credential_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(AiCredentialInvalidError):
        await _call(handler)


@pytest.mark.asyncio
async def test_non_json_reply_is_response_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion("sorry, I can't help with that"))

    with pytest.raises(AiResponseInvalidError):
        await _call(handler)


@pytest.mark.asyncio
async def test_timeout_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(AiUnavailableError, match="5s"):
        await _call(handler)


@pytest.mark.asyncio
async def test_list_models_keeps_modality() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://aihub.example/core-api/v1/models"
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-oss-120b", "modality": "chat", "capabilities": ["chat"]},
                    {"id": "Qwen3-Embedding-4B", "modality": "embedding"},
                    # Real NT AI Hub entries where modality and capabilities disagree:
                    {"id": "Qwen3.8-27B", "modality": "vision-ocr", "capabilities": ["chat"]},
                    {"id": "typhoon-asr", "modality": "chat", "capabilities": ["stt"]},
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        models = await OpenAICompatibleLlmClient(client=client).list_models(_CONNECTION)
    assert [(m.id, m.is_chat) for m in models] == [
        ("gpt-oss-120b", True),
        ("Qwen3-Embedding-4B", False),
        ("Qwen3.8-27B", True),
        ("typhoon-asr", False),
    ]


def test_connection_repr_hides_key() -> None:
    assert "secret-key" not in repr(_CONNECTION)
