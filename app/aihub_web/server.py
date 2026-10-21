"""HTTP layer: the JSON API, the NDJSON reply stream, and the static UI."""

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import httpx2
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import (
    JSONResponse,
    PlainTextResponse,
    Response,
    StreamingResponse,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from .catalog import Catalog
from .chat import ApiError, ChatRequest, ChatService
from .config import Config, secret
from .db import Store
from .images import PACING, GpuLock, ImageJobs, Pacing
from .providers import Provider, build_providers
from .providers.images import EngineClient
from .security import SecurityMiddleware

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CHAT_ID = re.compile(r"[0-9a-f]{16}")
IMAGE_ID = re.compile(r"[0-9a-f]{16}")
# A message may be 100,000 characters, which is under 400 KB even if every one takes four bytes.
BODY_LIMIT = 1024 * 1024
MAINTENANCE_INTERVAL = 60.0
PURGE_INTERVAL = 3600.0
# A system or environment proxy (httpx2 also reads the macOS one) must never capture calls to a local
# Ollama. A mount mapped to None sends matching URLs straight out; every other host keeps its proxy.
LOOPBACK_DIRECT = {
    "all://127.0.0.1": None,
    "all://localhost": None,
    "all://[::1]": None,
}

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".mjs")


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RenameChat(_Body):
    title: str = Field(min_length=1, max_length=200)


class Settings(_Body):
    instructions: str = Field(max_length=4000)


class DeleteAll(_Body):
    confirm: Literal["delete-all"]


class StopImage(_Body):
    """Nothing to say: the chat in the URL is the job to stop."""


@dataclass(slots=True)
class Context:
    cfg: Config
    store: Store
    catalog: Catalog
    service: ChatService


class Static(StaticFiles):
    """Application files revalidate on every load; vendored libraries are versioned, so they never do."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code in (200, 304):
            immutable = path.startswith("lib/")
            response.headers["Cache-Control"] = (
                "public, max-age=31536000, immutable" if immutable else "no-cache"
            )
        return response


def ctx_of(request: Request) -> Context:
    return request.app.state.ctx


async def read_json[T: BaseModel](request: Request, schema: type[T]) -> T:
    try:
        raw = await request.json()
    except ValueError as err:
        raise ApiError(400, "bad_json", "The request body isn't valid JSON.") from err
    try:
        return schema.model_validate(raw)
    except ValidationError as err:
        first = err.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "body"
        raise ApiError(422, "invalid", f"{where}: {first['msg']}") from err


def chat_or_404(ctx: Context, chat_id: str):
    chat = ctx.store.get_chat(chat_id) if CHAT_ID.fullmatch(chat_id) else None
    if chat is None:
        raise ApiError(404, "no_chat", "That chat no longer exists.")
    return chat


async def refresh_briefly(
    catalog: Catalog, timeout: float, *, force: bool = False
) -> None:
    """Refresh what is stale, but never make a page load wait on a slow provider."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(catalog.refresh(force=force), timeout)


def models_payload(ctx: Context) -> dict[str, object]:
    return {
        "models": [m.to_json() for m in ctx.catalog.models()],
        "default_model": ctx.catalog.default_model(),
        "providers": ctx.catalog.status,
    }


async def health(request: Request) -> Response:
    return JSONResponse({"ok": True})


async def bootstrap(request: Request) -> Response:
    ctx = ctx_of(request)
    await refresh_briefly(ctx.catalog, 1.0)
    return JSONResponse(
        {
            **models_payload(ctx),
            "chats": [c.to_json() for c in ctx.store.list_chats()],
            "settings": {"instructions": ctx.store.get_setting("instructions")},
            "retention_days": ctx.cfg.retention_days,
        }
    )


async def models(request: Request) -> Response:
    ctx = ctx_of(request)
    await refresh_briefly(
        ctx.catalog, 8.0, force=request.query_params.get("refresh") == "1"
    )
    return JSONResponse(models_payload(ctx))


async def list_chats(request: Request) -> Response:
    ctx = ctx_of(request)
    query = request.query_params.get("q", "")[:200]
    return JSONResponse({"chats": [c.to_json() for c in ctx.store.list_chats(query)]})


async def get_chat(request: Request) -> Response:
    ctx = ctx_of(request)
    chat = chat_or_404(ctx, request.path_params["chat_id"])
    body: dict[str, object] = {
        "chat": chat.to_json(),
        "messages": [m.to_json() for m in ctx.store.messages(chat.id)],
    }
    if ctx.service.images:
        # An image still being made: a window opened later can show where it is.
        body["pending_image"] = ctx.service.images.pending(chat.id)
    return JSONResponse(body)


async def get_image(request: Request) -> Response:
    ctx = ctx_of(request)
    # The page asks for its images itself, so a request that says another site made it is not the page's.
    if request.headers.get("sec-fetch-site") not in (None, "same-origin", "none"):
        return PlainTextResponse("Cross-site request refused.", status_code=403)
    chat_id, image_id = request.path_params["chat_id"], request.path_params["image_id"]
    found = (
        ctx.store.get_image(chat_id, image_id)
        if CHAT_ID.fullmatch(chat_id) and IMAGE_ID.fullmatch(image_id)
        else None
    )
    if found is None:
        raise ApiError(404, "no_image", "That image no longer exists.")
    # The type is fixed, not read from the row or the bytes: nothing the engine returns picks it.
    return Response(
        found[1],
        media_type="image/webp",
        headers={"Content-Disposition": f'inline; filename="aihub-{image_id}.webp"'},
    )


async def stop_image(request: Request) -> Response:
    ctx = ctx_of(request)
    chat = chat_or_404(ctx, request.path_params["chat_id"])
    if (await request.body()).strip():
        await read_json(request, StopImage)
    if not (ctx.service.images and ctx.service.images.stop(chat.id)):
        raise ApiError(404, "no_job", "There is no image being made in this chat.")
    return JSONResponse({"stopping": True})


async def rename_chat(request: Request) -> Response:
    ctx = ctx_of(request)
    chat = chat_or_404(ctx, request.path_params["chat_id"])
    body = await read_json(request, RenameChat)
    updated = ctx.store.update_chat(chat.id, {"title": " ".join(body.title.split())})
    return JSONResponse(updated.to_json() if updated else {})


async def delete_chat(request: Request) -> Response:
    ctx = ctx_of(request)
    chat = chat_or_404(ctx, request.path_params["chat_id"])
    ctx.store.delete_chat(chat.id)
    return Response(status_code=204)


async def delete_all_chats(request: Request) -> Response:
    ctx = ctx_of(request)
    await read_json(request, DeleteAll)
    return JSONResponse({"deleted": ctx.store.delete_all()})


async def put_settings(request: Request) -> Response:
    ctx = ctx_of(request)
    body = await read_json(request, Settings)
    ctx.store.set_setting("instructions", body.instructions.strip())
    return JSONResponse({"instructions": body.instructions.strip()})


async def ndjson(
    events: AsyncGenerator[dict[str, object], None],
) -> AsyncIterator[bytes]:
    try:
        async for event in events:
            yield (
                json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode()
    finally:
        # A dropped connection must still run the generator's cleanup, which stores the partial reply.
        await events.aclose()


async def post_chat(request: Request) -> Response:
    ctx = ctx_of(request)
    body = await read_json(request, ChatRequest)
    turn = ctx.service.prepare(body)
    return StreamingResponse(
        ndjson(ctx.service.stream(turn)),
        media_type="application/x-ndjson",
        # Not "no-store": on a streamed response Chromium reports the finished fetch as cancelled.
        headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
    )


async def api_not_found(request: Request) -> Response:
    raise ApiError(404, "not_found", "There is no such API endpoint.")


async def api_error(request: Request, err: Exception) -> Response:
    assert isinstance(err, ApiError)
    return JSONResponse(
        {"error": {"code": err.code, "message": err.message}}, status_code=err.status
    )


async def maintenance(ctx: Context) -> None:
    """Keep the model list fresh and delete expired chats, for as long as the app runs."""
    last_purge = time.monotonic()
    while True:
        await asyncio.sleep(MAINTENANCE_INTERVAL)
        try:
            await ctx.catalog.refresh()
            if time.monotonic() - last_purge >= PURGE_INTERVAL:
                ctx.store.purge_expired(ctx.cfg.retention_days)
                last_purge = time.monotonic()
        except Exception:
            log.exception("maintenance pass failed")


def create_app(
    cfg: Config,
    *,
    environ: Mapping[str, str] = os.environ,
    transport: httpx2.AsyncBaseTransport | None = None,
    providers: Mapping[str, Provider] | None = None,
    today=date.today,
    image_pacing: Pacing = PACING,
) -> Starlette:
    """`transport`, `providers` and `image_pacing` exist so tests can swap the network and the models out
    and make image jobs look at the engine more often."""

    # Set by the desktop shell, which gives only its own window the matching cookie; the service sets none.
    launch_token = secret("AIHUB_LAUNCH_TOKEN", environ)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        client = httpx2.AsyncClient(transport=transport, mounts=LOOPBACK_DIRECT)
        store = Store(cfg.state_dir / "chats.db")
        catalog = Catalog(cfg, client, environ=environ, today=today)
        # Image generation exists only where the settings name the engine's socket.
        images = (
            ImageJobs(
                store,
                EngineClient(cfg.images.socket),
                GpuLock(Path(cfg.images.socket).parent),
                client,
                cfg.ollama,
                pacing=image_pacing,
            )
            if cfg.images.socket
            else None
        )
        service = ChatService(
            cfg,
            store,
            catalog,
            {**build_providers(cfg, client, environ), **(providers or {})},
            today=today,
            images=images,
        )
        ctx = app.state.ctx = Context(cfg, store, catalog, service)
        store.purge_expired(cfg.retention_days)
        await refresh_briefly(catalog, 6.0, force=True)
        task = asyncio.create_task(maintenance(ctx))
        try:
            yield
        finally:
            if images:
                # Images in the making are saved as stopped; the engine is left to finish and idle out.
                await images.shutdown()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await client.aclose()
            store.close()

    return Starlette(
        routes=[
            Route("/api/health", health),
            Route("/api/bootstrap", bootstrap),
            Route("/api/models", models),
            Route("/api/chat", post_chat, methods=["POST"]),
            Route("/api/chats", list_chats),
            Route("/api/chats", delete_all_chats, methods=["DELETE"]),
            Route("/api/chats/{chat_id}", get_chat),
            Route("/api/chats/{chat_id}", rename_chat, methods=["PATCH"]),
            Route("/api/chats/{chat_id}", delete_chat, methods=["DELETE"]),
            Route("/api/chats/{chat_id}/images/{image_id}", get_image),
            Route("/api/chats/{chat_id}/image/stop", stop_image, methods=["POST"]),
            Route("/api/settings", put_settings, methods=["PUT"]),
            Route(
                "/api/{rest:path}",
                api_not_found,
                methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            ),
            # Only the static files are compressed: a gzip buffer would hold back streamed tokens.
            Mount(
                "/",
                app=GZipMiddleware(
                    Static(directory=STATIC_DIR, html=True),
                    minimum_size=1024,
                    compresslevel=6,
                ),
                name="ui",
            ),
        ],
        middleware=[
            Middleware(
                SecurityMiddleware,
                allowed_hosts=cfg.allowed_hosts,
                token=launch_token.encode() if launch_token else None,
            )
        ],
        exception_handlers={ApiError: api_error},
        lifespan=lifespan,
        max_body_size=BODY_LIMIT,
    )
