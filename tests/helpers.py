"""Shared test doubles: model data captured from real servers, and a network stub that serves it."""

import asyncio
import json
from datetime import date
from pathlib import Path

import httpx2
from aihub_web.catalog import Catalog
from aihub_web.chat import ChatService
from aihub_web.config import Config
from aihub_web.db import Store
from aihub_web.providers import Finish, Text

FIXTURES = Path(__file__).parent / "fixtures"
OLLAMA = json.loads((FIXTURES / "ollama_models.json").read_text())
OPENROUTER = json.loads((FIXTURES / "openrouter_models.json").read_text())
TODAY = date(2026, 10, 20)
KEYS = {"AIHUB_OPENROUTER_KEY": "sk-or-test", "AIHUB_ANTHROPIC_KEY": "sk-ant-test"}


def handler_for(
    *, ollama_up: bool = True, openrouter_up: bool = True, calls: list | None = None
):
    """A MockTransport handler for Ollama's model endpoints and OpenRouter's public model list."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.host == "127.0.0.1":
            if not ollama_up:
                raise httpx2.ConnectError("refused", request=request)
            if request.url.path == "/api/tags":
                return httpx2.Response(200, json={"models": OLLAMA["tags"]})
            if request.url.path == "/api/show":
                return httpx2.Response(
                    200, json=OLLAMA["show"][json.loads(request.content)["model"]]
                )
        if request.url.host == "openrouter.ai" and request.url.path == "/api/v1/models":
            if not openrouter_up:
                raise httpx2.ConnectError("offline", request=request)
            return httpx2.Response(200, json={"data": OPENROUTER})
        return httpx2.Response(404)

    return handle


HANG = object()


class FakeProvider:
    """Plays a script as a provider's events.

    Items are events to yield, exceptions to raise, callables to run (for side effects such as
    advancing a clock), or HANG to wait until cancelled.
    """

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.requests: list = []

    async def stream(self, req):
        self.requests.append(req)
        for item in list(self.script):
            if item is HANG:
                await asyncio.Event().wait()
            elif isinstance(item, BaseException):
                raise item
            elif callable(item):
                item()
            else:
                yield item


class Clock:
    """A hand-wound clock, for both wall time and monotonic time."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float = 0, *, days: float = 0) -> None:
        self.now += seconds + days * 86_400


async def make_service(tmp_path: Path, *, clock: "Clock | None" = None, **config):
    """A ChatService over a real store and the stubbed model catalog, with a FakeProvider per provider."""
    providers = {
        "openrouter": FakeProvider(Text("ok"), Finish("stop")),
        "ollama": FakeProvider(Text("ok"), Finish("stop")),
        "anthropic": FakeProvider(Text("ok"), Finish("stop")),
    }
    cfg = Config(state_dir=tmp_path, **config)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler_for()))
    store = Store(tmp_path / "chats.db")
    catalog = Catalog(cfg, client, environ=KEYS, today=lambda: TODAY)
    await catalog.refresh()
    service = ChatService(
        cfg, store, catalog, providers, today=lambda: TODAY, monotonic=clock or Clock()
    )
    return service, store, providers
