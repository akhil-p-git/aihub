"""What every provider adapter shares: the event types, the error type and HTTP error mapping."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

import httpx2

from ..catalog import ModelInfo


@dataclass(frozen=True, slots=True)
class Text:
    text: str


@dataclass(frozen=True, slots=True)
class Thinking:
    text: str


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: Decimal | None = None  # only when the provider reports it
    tokens_per_second: float | None = None


@dataclass(frozen=True, slots=True)
class Finish:
    reason: str


Event = Text | Thinking | Usage | Finish


class ProviderError(Exception):
    """A provider call failed. `code` picks the next step the UI offers; `message` is shown as written."""

    def __init__(
        self, message: str, code: str = "upstream", status: int | None = None
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


@dataclass(frozen=True, slots=True)
class GenRequest:
    model: ModelInfo
    messages: list[dict[str, str]]  # alternating user/assistant, starting with user
    system: str
    effort: str | None
    max_tokens: int


class Provider(Protocol):
    def stream(self, req: GenRequest) -> AsyncIterator[Event]: ...


@dataclass(frozen=True, slots=True)
class ImageRequest:
    """One image for the engine. The seed is always explicit: the engine's own default is a fixed 42."""

    model_id: str  # the engine's name for the model, e.g. "zimage"
    prompt: str
    width: int
    height: int
    seed: int
    steps: int
    sample_method: str | None
    reference_webp: bytes | None = None  # an image to edit


@dataclass(frozen=True, slots=True)
class Progress:
    """Where an image job is: waiting, starting, queued, sampling or finishing (step and steps only while sampling)."""

    phase: str
    step: int | None = None
    steps: int | None = None


STATUS_CODES = {
    400: "bad_request",
    401: "auth",
    402: "quota",
    403: "auth",
    404: "model_not_found",
    408: "timeout",
    413: "bad_request",
    429: "rate_limit",
    503: "routing",
    504: "timeout",
    529: "overloaded",
}

SUMMARIES = {
    "auth": "{label} rejected the API key ({status}). Check it in ~/.config/aihub/aihub.env.",
    "quota": "{label} reports no credit or spend limit left ({status}).",
    "rate_limit": "{label} is rate limiting this key ({status}).",
    "model_not_found": "{label} doesn't have that model ({status}).",
    "overloaded": "{label} is overloaded ({status}). Try again in a moment.",
    "routing": "{label} has no provider that fits the routing settings ({status}).",
    "timeout": "{label} timed out ({status}).",
    "bad_request": "{label} rejected the request ({status}).",
    "upstream": "{label} had a server error ({status}).",
}

SPEND_LIMIT_HINTS = ("usage limit", "spend limit", "enforced_spend_limit")


def _detail(body: str) -> str:
    """The provider's own explanation, from the usual JSON error shapes or the raw text."""
    try:
        data = json.loads(body)
    except ValueError:
        return " ".join(body.split())[:300]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        error = error.get("message")
    if not isinstance(error, str) and isinstance(data, dict):
        error = data.get("message")
    return " ".join(str(error or "").split())[:300]


def error_from_body(status: int, body: str, label: str) -> ProviderError:
    code = STATUS_CODES.get(status, "upstream")
    if any(hint in body.lower() for hint in SPEND_LIMIT_HINTS):
        code = "quota"
    summary = SUMMARIES[code].format(label=label, status=status)
    return ProviderError(f"{summary} {_detail(body)}".strip(), code, status)


async def error_from_response(response: httpx2.Response, label: str) -> ProviderError:
    body = (await response.aread())[:8192].decode("utf-8", "replace")
    return error_from_body(response.status_code, body, label)
