"""OpenRouter's OpenAI-compatible chat completions, streamed as server-sent events."""

import json
from collections.abc import AsyncIterator, Mapping
from decimal import Decimal, InvalidOperation

import httpx2

from ..config import OpenRouterConfig, secret
from .base import (
    Event,
    Finish,
    GenRequest,
    ProviderError,
    Text,
    Thinking,
    Usage,
    error_from_body,
    error_from_response,
)

TIMEOUT = httpx2.Timeout(connect=5.0, read=120.0, write=30.0, pool=10.0)


def request_body(req: GenRequest, cfg: OpenRouterConfig) -> dict[str, object]:
    messages = list(req.messages)
    if req.system:
        messages.insert(0, {"role": "system", "content": req.system})
    body: dict[str, object] = {
        "model": req.model.native,
        "messages": messages,
        "stream": True,
        "max_tokens": req.max_tokens,
    }
    if req.model.efforts and req.effort:
        body["reasoning"] = {"effort": "none" if req.effort == "off" else req.effort}
    if req.model.tier == "paid":
        body["provider"] = cfg.paid_provider
    return body


def _reasoning_text(delta: Mapping[str, object]) -> str:
    """Reasoning arrives as `reasoning` (or its alias), else as typed `reasoning_details` entries."""
    for key in ("reasoning", "reasoning_content"):
        if isinstance(delta.get(key), str):
            return str(delta[key])
    details = delta.get("reasoning_details")
    if not isinstance(details, list):
        return ""
    return "".join(
        part
        for entry in details
        if isinstance(entry, dict)
        for part in (entry.get("text") or entry.get("summary"),)
        if isinstance(part, str)
    )


def parse_chunk(obj: Mapping[str, object]) -> list[Event]:
    if error := obj.get("error"):
        # After the first byte the HTTP status stays 200; the failure is a chunk of its own.
        code = error.get("code") if isinstance(error, dict) else None
        raise error_from_body(
            code if isinstance(code, int) else 502, json.dumps(obj), "OpenRouter"
        )
    events: list[Event] = []
    for choice in obj.get("choices") or []:
        delta = choice.get("delta") or {}
        if thinking := _reasoning_text(delta):
            events.append(Thinking(thinking))
        if isinstance(delta.get("content"), str) and delta["content"]:
            events.append(Text(delta["content"]))
        if reason := choice.get("finish_reason"):
            events.append(Finish(reason))
    if usage := obj.get("usage"):
        try:
            cost = (
                Decimal(str(usage["cost"])) if usage.get("cost") is not None else None
            )
        except InvalidOperation:
            cost = None
        events.append(
            Usage(usage.get("prompt_tokens"), usage.get("completion_tokens"), cost)
        )
    return events


class OpenRouterProvider:
    def __init__(
        self,
        cfg: OpenRouterConfig,
        client: httpx2.AsyncClient,
        environ: Mapping[str, str],
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._environ = environ

    async def stream(self, req: GenRequest) -> AsyncIterator[Event]:
        key = secret(self._cfg.key_env, self._environ)
        if not key:
            raise ProviderError(f"{self._cfg.key_env} is not set.", "no_key")
        headers = {"Authorization": f"Bearer {key}", "Accept": "text/event-stream"}
        try:
            async with self._client.stream(
                "POST",
                f"{self._cfg.url}/chat/completions",
                json=request_body(req, self._cfg),
                headers=headers,
                timeout=TIMEOUT,
            ) as response:
                if response.status_code >= 400:
                    raise await error_from_response(response, "OpenRouter")
                async for line in response.aiter_lines():
                    # Blank lines separate events and ":" lines are keep-alive comments.
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except ValueError as err:
                        raise ProviderError(
                            "OpenRouter sent a chunk that isn't JSON."
                        ) from err
                    for event in parse_chunk(chunk):
                        yield event
        except httpx2.ConnectError as err:
            raise ProviderError("Can't reach OpenRouter.", "unreachable") from err
        except httpx2.TimeoutException as err:
            raise ProviderError("OpenRouter stopped responding.", "timeout") from err
