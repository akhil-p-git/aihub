"""Ollama's native /api/chat: NDJSON lines carrying `message.thinking` and `message.content`."""

import json
from collections.abc import AsyncIterator

import httpx2

from ..config import OllamaConfig
from .base import (
    Event,
    Finish,
    GenRequest,
    ProviderError,
    Text,
    Thinking,
    Usage,
    error_from_response,
)

# Loading a large model can take a while before the first token, so reads wait a long time.
TIMEOUT = httpx2.Timeout(connect=3.0, read=300.0, write=30.0, pool=5.0)


def request_body(req: GenRequest, cfg: OllamaConfig) -> dict[str, object]:
    model = req.model
    messages = list(req.messages)
    if req.system:
        messages.insert(0, {"role": "system", "content": req.system})
    body: dict[str, object] = {
        "model": model.native,
        "messages": messages,
        "stream": True,
        "keep_alive": cfg.keep_alive,
        "options": {
            "num_ctx": min(cfg.num_ctx, model.ctx_max or cfg.num_ctx),
            "num_predict": req.max_tokens,
        },
    }
    if model.efforts and req.effort:
        body["think"] = {"off": False, "on": True}.get(req.effort, req.effort)
    return body


def parse_line(line: str) -> list[Event]:
    try:
        obj = json.loads(line)
    except ValueError as err:
        raise ProviderError("Ollama sent a line that isn't JSON.") from err
    if "error" in obj:
        raise ProviderError(f"Ollama: {obj['error']}")
    events: list[Event] = []
    message = obj.get("message") or {}
    if thinking := message.get("thinking"):
        events.append(Thinking(thinking))
    if content := message.get("content"):
        events.append(Text(content))
    if obj.get("done"):
        count, nanoseconds = obj.get("eval_count"), obj.get("eval_duration")
        speed = count / (nanoseconds / 1e9) if count and nanoseconds else None
        events.append(Usage(obj.get("prompt_eval_count"), count, None, speed))
        events.append(Finish(obj.get("done_reason") or "stop"))
    return events


class OllamaProvider:
    def __init__(self, cfg: OllamaConfig, client: httpx2.AsyncClient) -> None:
        self._cfg = cfg
        self._client = client

    async def stream(self, req: GenRequest) -> AsyncIterator[Event]:
        try:
            async with self._client.stream(
                "POST",
                f"{self._cfg.url}/api/chat",
                json=request_body(req, self._cfg),
                timeout=TIMEOUT,
            ) as response:
                if response.status_code >= 400:
                    raise await error_from_response(response, "Ollama")
                async for line in response.aiter_lines():
                    if line:
                        for event in parse_line(line):
                            yield event
        except httpx2.ConnectError as err:
            raise ProviderError(
                f"Can't reach Ollama at {self._cfg.url}. Start it with `systemctl --user start ollama`.",
                "unreachable",
            ) from err
        except httpx2.TimeoutException as err:
            raise ProviderError("Ollama stopped responding.", "timeout") from err
