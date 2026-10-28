"""LlmClient for any OpenAI-compatible chat-completions gateway (NT AI Hub / MATCHA, vLLM,
Ollama's /v1, ...). Plain httpx — no vendor SDK, same as every other outbound client here.

`base_url` is the API root that `/chat/completions` and `/models` hang off, e.g.
https://aihub.ntaisolution.com/core-api/v1. Verified against NT AI Hub on 2026-10-26:
`gpt-oss-120b` honours `response_format: json_schema` (strict); a server that rejects
`response_format` with HTTP 400 is retried once without it, relying on the prompt alone.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from src.application.ai.ports import AiConnection, LlmClient, LlmModelInfo
from src.shared.errors import (
    AiCredentialInvalidError,
    AiResponseInvalidError,
    AiUnavailableError,
)

_MAX_TOKENS = 800
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_object(content: str) -> dict[str, Any]:
    """Some models wrap JSON in prose, ``` fences, or a <think> block even when asked not to —
    take the outermost {...} and require it to be an object."""
    stripped = _THINK_BLOCK.sub("", content)
    match = _JSON_OBJECT.search(stripped)
    if match is None:
        raise AiResponseInvalidError("LLM reply contained no JSON object")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise AiResponseInvalidError("LLM reply was not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise AiResponseInvalidError("LLM reply JSON was not an object")
    return parsed


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        if "text/html" in response.headers.get("content-type", ""):
            raise AiUnavailableError(
                "base URL returned a web page, not the API — check the path "
                "(for NT AI Hub it ends in /core-api/v1)"
            )
        return
    if response.status_code in (401, 403):
        raise AiCredentialInvalidError(
            f"gateway rejected the API key (HTTP {response.status_code})"
        )
    if response.status_code == 404 and "text/html" in response.headers.get("content-type", ""):
        raise AiUnavailableError(
            "base URL returned a web page, not the API — check the path "
            "(for NT AI Hub it ends in /core-api/v1)"
        )
    raise AiUnavailableError(f"gateway returned HTTP {response.status_code}: {response.text[:200]}")


class OpenAICompatibleLlmClient(LlmClient):
    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        """`client` is injectable for tests (httpx.MockTransport); production passes none and
        gets a fresh client per call with the connection's own timeout."""
        self._client = client

    async def _request(
        self,
        connection: AiConnection,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{connection.base_url.rstrip('/')}{path}"
        headers = {"Authorization": f"Bearer {connection.api_key}"}
        owns_client = self._client is None
        http_client = self._client or httpx.AsyncClient(timeout=connection.timeout_seconds)
        try:
            return await http_client.request(method, url, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise AiUnavailableError(
                f"gateway did not answer within {connection.timeout_seconds}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise AiUnavailableError(f"could not reach gateway: {type(exc).__name__}") from exc
        finally:
            if owns_client:
                await http_client.aclose()

    async def complete_json(
        self,
        connection: AiConnection,
        *,
        system_prompt: str,
        user_message: str,
        schema_name: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": connection.model,
            "temperature": 0,
            "max_tokens": _MAX_TOKENS,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
        }
        response = await self._request(connection, "POST", "/chat/completions", json_body=body)
        if response.status_code == 400 and "response_format" in response.text:
            body.pop("response_format")
            response = await self._request(connection, "POST", "/chat/completions", json_body=body)
        _raise_for_status(response)

        try:
            payload = response.json()
            content = payload["choices"][0]["message"].get("content") or ""
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise AiResponseInvalidError("gateway reply was not a chat completion") from exc
        return _extract_json_object(content)

    async def list_models(self, connection: AiConnection) -> list[LlmModelInfo]:
        response = await self._request(connection, "GET", "/models")
        _raise_for_status(response)
        try:
            data = response.json()["data"]
            return [
                LlmModelInfo(
                    id=str(item["id"]),
                    modality=item.get("modality"),
                    capabilities=(
                        [str(c) for c in item["capabilities"]]
                        if isinstance(item.get("capabilities"), list)
                        else None
                    ),
                )
                for item in data
                if isinstance(item, dict) and "id" in item
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise AiUnavailableError("gateway /models reply was not a model list") from exc
