"""Anthropic's native Messages API, streamed as server-sent events (not the OpenAI-compatible layer)."""

import json
from collections.abc import AsyncIterator, Mapping

import httpx2

from ..config import AnthropicConfig, secret
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

API_VERSION = "2023-06-01"
TIMEOUT = httpx2.Timeout(connect=5.0, read=120.0, write=30.0, pool=10.0)

# stop_reason values that mean the reply was cut short; the UI knows them as finish "length".
CUT_OFF = frozenset({"max_tokens", "model_context_window_exceeded"})

IN_STREAM_ERRORS = {
    "authentication_error": 401,
    "permission_error": 403,
    "not_found_error": 404,
    "request_too_large": 413,
    "rate_limit_error": 429,
    "overloaded_error": 529,
    "invalid_request_error": 400,
}


def request_body(req: GenRequest, cfg: AnthropicConfig) -> dict[str, object]:
    body: dict[str, object] = {
        "model": req.model.native,
        "max_tokens": req.max_tokens,
        "stream": True,
        "messages": req.messages,
    }
    if req.system:
        body["system"] = req.system
    if req.effort == "off":
        # Not every model can switch thinking off, and each takes a different mode; the catalog
        # only offers "off" where the configured model has one.
        configured = next((m for m in cfg.models if m.id == req.model.native), None)
        if configured and configured.thinking_off:
            body["thinking"] = {"type": configured.thinking_off}
    else:
        # Thinking text is hidden unless a summary is asked for.
        body["thinking"] = {"type": "adaptive", "display": "summarized"}
        if req.effort:
            body["output_config"] = {"effort": req.effort}
    return body


class StreamParser:
    """Turns Messages API events into provider events; usage is spread over two of them."""

    def __init__(self) -> None:
        self._usage: dict[str, int] = {}

    def feed(self, data: Mapping[str, object]) -> list[Event]:
        kind = data.get("type")
        if kind == "error":
            error = data.get("error") or {}
            status = IN_STREAM_ERRORS.get(str(error.get("type")), 500)
            raise error_from_body(status, json.dumps(data), "Anthropic")
        if kind == "message_start":
            self._merge((data.get("message") or {}).get("usage"))
        elif kind == "content_block_delta":
            delta = data.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                return [Text(delta["text"])]
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                return [Thinking(delta["thinking"])]
        elif kind == "message_delta":
            self._merge(data.get("usage"))
            reason = (data.get("delta") or {}).get("stop_reason")
            return [Finish("length" if reason in CUT_OFF else reason)] if reason else []
        elif kind == "message_stop":
            billed_in = sum(
                self._usage.get(key, 0)
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
            return [Usage(billed_in or None, self._usage.get("output_tokens"))]
        return []

    def _merge(self, usage: object) -> None:
        if isinstance(usage, dict):
            self._usage.update({k: v for k, v in usage.items() if isinstance(v, int)})


class AnthropicProvider:
    def __init__(
        self,
        cfg: AnthropicConfig,
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
        headers = {"x-api-key": key, "anthropic-version": API_VERSION}
        parser = StreamParser()
        try:
            async with self._client.stream(
                "POST",
                f"{self._cfg.url}/v1/messages",
                json=request_body(req, self._cfg),
                headers=headers,
                timeout=TIMEOUT,
            ) as response:
                if response.status_code >= 400:
                    raise await error_from_response(response, "Anthropic")
                async for line in response.aiter_lines():
                    # The "event:" line repeats the "type" inside the data, so only data lines matter.
                    if not line.startswith("data:"):
                        continue
                    try:
                        data = json.loads(line[5:])
                    except ValueError as err:
                        raise ProviderError(
                            "Anthropic sent an event that isn't JSON."
                        ) from err
                    for event in parser.feed(data):
                        yield event
        except httpx2.ConnectError as err:
            raise ProviderError("Can't reach Anthropic.", "unreachable") from err
        except httpx2.TimeoutException as err:
            raise ProviderError("Anthropic stopped responding.", "timeout") from err
