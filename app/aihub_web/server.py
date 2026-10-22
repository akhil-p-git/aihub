"""HTTP layer: the JSON API, the NDJSON reply stream, and the static UI."""

import asyncio
import contextlib
import json
import logging
import mimetypes
import os
import re
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import httpx2
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
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
from .chat import TASK_MODEL_MESSAGE, ApiError, ChatRequest, ChatService, runs_tasks
from .config import Config, secret
from .db import Project, Store, Task
from .images import PACING, GpuLock, ImageJobs, Pacing
from .providers import Provider, build_providers
from .providers.images import EngineClient
from .schedule import Schedule, describe, next_run, parse_schedule
from .security import SecurityMiddleware
from .tasks import SWITCHED_OFF, TICK_SECONDS, RunResult, Scheduler

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
CHAT_ID = re.compile(r"[0-9a-f]{16}")
IMAGE_ID = re.compile(r"[0-9a-f]{16}")
PROJECT_ID = re.compile(r"[0-9a-f]{16}")
TASK_ID = re.compile(r"[0-9a-f]{16}")
# What a person may write: the same room for the standing instructions and for a project's.
INSTRUCTIONS_MAX = 4000
PROJECT_NAME_MAX = 80
MAX_PROJECTS = 100
TASK_NAME_MAX = 80
MAX_TASKS = 50
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


def _one_line(value: str, longest: int, what: str) -> str:
    """Whitespace collapsed, and between 1 and `longest` characters."""
    cleaned = " ".join(value.split())
    if not 1 <= len(cleaned) <= longest:
        raise ValueError(f"{what} must be 1 to {longest} characters")
    return cleaned


class EditChat(_Body):
    """A chat's title and its project. A key that is absent is left alone; project_id null takes the chat out."""

    title: str | None = Field(None, max_length=200)
    project_id: str | None = Field(None, pattern=r"^[0-9a-f]{16}$")

    @model_validator(mode="after")
    def _something_to_change(self) -> "EditChat":
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        if "title" in self.model_fields_set:
            if self.title is None:
                raise ValueError("title can't be null")
            _one_line(self.title, 200, "title")
        return self


class NewProject(_Body):
    name: str
    instructions: str = Field("", max_length=INSTRUCTIONS_MAX)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _one_line(value, PROJECT_NAME_MAX, "name")


class EditProject(_Body):
    name: str | None = None
    instructions: str | None = Field(None, max_length=INSTRUCTIONS_MAX)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name can't be null")
        return _one_line(value, PROJECT_NAME_MAX, "name")

    @field_validator("instructions")
    @classmethod
    def _instructions(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("instructions can't be null")
        return value

    @model_validator(mode="after")
    def _something_to_change(self) -> "EditProject":
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        return self


class DropProject(_Body):
    """What becomes of a project's chats: they stay, without a project, or they go with it."""

    chats: Literal["keep", "delete"] = "keep"


def _prompt_text(value: str) -> str:
    if not value.strip():
        raise ValueError("prompt is empty")
    return value


class NewTask(_Body):
    name: str
    prompt: str
    model: str = Field(min_length=1, max_length=200)
    effort: str | None = Field(None, max_length=16)
    schedule: Schedule
    project_id: str | None = Field(None, pattern=r"^[0-9a-f]{16}$")
    enabled: bool = Field(True, strict=True)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _one_line(value, TASK_NAME_MAX, "name")

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str) -> str:
        return _prompt_text(value)


class EditTask(_Body):
    """A key that is absent is left alone. effort and project_id may be null (the model's usual level, no
    project); the others may not."""

    name: str | None = None
    prompt: str | None = None
    model: str | None = Field(None, min_length=1, max_length=200)
    effort: str | None = Field(None, max_length=16)
    schedule: Schedule | None = None
    project_id: str | None = Field(None, pattern=r"^[0-9a-f]{16}$")
    enabled: bool | None = Field(None, strict=True)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("name can't be null")
        return _one_line(value, TASK_NAME_MAX, "name")

    @field_validator("prompt")
    @classmethod
    def _prompt(cls, value: str | None) -> str:
        if value is None:
            raise ValueError("prompt can't be null")
        return _prompt_text(value)

    @model_validator(mode="after")
    def _something_to_change(self) -> "EditTask":
        if not self.model_fields_set:
            raise ValueError("nothing to change")
        for key in ("model", "schedule", "enabled"):
            if key in self.model_fields_set and getattr(self, key) is None:
                raise ValueError(f"{key} can't be null")
        return self


class Settings(_Body):
    instructions: str = Field(max_length=INSTRUCTIONS_MAX)


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
    scheduler: Scheduler
    clock: Callable[[], float]

    def now_ms(self) -> int:
        return round(self.clock() * 1000)


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


def project_or_404(ctx: Context, project_id: str) -> Project:
    project = (
        ctx.store.get_project(project_id) if PROJECT_ID.fullmatch(project_id) else None
    )
    if project is None:
        raise ApiError(404, "no_project", "That project no longer exists.")
    return project


def task_or_404(ctx: Context, task_id: str) -> Task:
    task = ctx.store.get_task(task_id) if TASK_ID.fullmatch(task_id) else None
    if task is None:
        raise ApiError(404, "no_task", "That task no longer exists.")
    return task


def task_json(ctx: Context, task: Task) -> dict[str, object]:
    """The task, and how its schedule reads and whether it is running now. A schedule this code cannot read
    must not stop the page from loading, so it reads as such."""
    try:
        words = describe(parse_schedule(task.schedule))
    except ValueError:
        words = "Unreadable schedule"
    return {
        **task.to_json(),
        "schedule_text": words,
        "running": task.id in ctx.scheduler.running,
    }


def checked_prompt(ctx: Context, prompt: str) -> str:
    cleaned = prompt.strip()
    if len(cleaned) > ctx.cfg.limits.max_message_chars:
        raise ApiError(
            413,
            "too_long",
            f"A prompt is limited to {ctx.cfg.limits.max_message_chars:,} characters.",
        )
    return cleaned


def check_task_model(ctx: Context, model_id: str) -> None:
    model = ctx.catalog.get(model_id)
    if model is None:
        raise ApiError(400, "unknown_model", f"There is no model called {model_id}.")
    if not runs_tasks(model):
        raise ApiError(422, "task_model", TASK_MODEL_MESSAGE)


def check_project(ctx: Context, project_id: str | None) -> None:
    if project_id and ctx.store.get_project(project_id) is None:
        raise ApiError(404, "no_project", "That project no longer exists.")


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
            "projects": [p.to_json() for p in ctx.store.list_projects()],
            "tasks": [task_json(ctx, t) for t in ctx.store.list_tasks()],
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


async def patch_chat(request: Request) -> Response:
    ctx = ctx_of(request)
    chat = chat_or_404(ctx, request.path_params["chat_id"])
    body = await read_json(request, EditChat)
    changes: dict[str, str | None] = {}
    if "title" in body.model_fields_set:
        changes["title"] = " ".join((body.title or "").split())
    if "project_id" in body.model_fields_set:
        if body.project_id and ctx.store.get_project(body.project_id) is None:
            raise ApiError(404, "no_project", "That project no longer exists.")
        changes["project_id"] = body.project_id
    updated = ctx.store.update_chat(chat.id, changes)
    return JSONResponse(updated.to_json() if updated else {})


async def list_projects(request: Request) -> Response:
    ctx = ctx_of(request)
    return JSONResponse({"projects": [p.to_json() for p in ctx.store.list_projects()]})


async def create_project(request: Request) -> Response:
    ctx = ctx_of(request)
    body = await read_json(request, NewProject)
    if ctx.store.count_projects() >= MAX_PROJECTS:
        raise ApiError(
            409,
            "too_many_projects",
            f"You can have {MAX_PROJECTS} projects. Delete one to make room.",
        )
    project = ctx.store.create_project(body.name, body.instructions.strip())
    return JSONResponse(project.to_json(), status_code=201)


async def patch_project(request: Request) -> Response:
    ctx = ctx_of(request)
    project = project_or_404(ctx, request.path_params["project_id"])
    body = await read_json(request, EditProject)
    updated = ctx.store.update_project(
        project.id,
        name=body.name,
        instructions=None if body.instructions is None else body.instructions.strip(),
    )
    return JSONResponse(updated.to_json() if updated else {})


async def delete_project(request: Request) -> Response:
    ctx = ctx_of(request)
    project = project_or_404(ctx, request.path_params["project_id"])
    body = (
        await read_json(request, DropProject)
        if (await request.body()).strip()
        else DropProject()
    )
    ctx.store.delete_project(project.id, delete_chats=body.chats == "delete")
    return Response(status_code=204)


async def list_tasks(request: Request) -> Response:
    ctx = ctx_of(request)
    return JSONResponse({"tasks": [task_json(ctx, t) for t in ctx.store.list_tasks()]})


async def create_task(request: Request) -> Response:
    ctx = ctx_of(request)
    body = await read_json(request, NewTask)
    if ctx.store.count_tasks() >= MAX_TASKS:
        raise ApiError(
            409,
            "too_many_tasks",
            f"You can have {MAX_TASKS} scheduled tasks. Delete one to make room.",
        )
    prompt = checked_prompt(ctx, body.prompt)
    check_task_model(ctx, body.model)
    check_project(ctx, body.project_id)
    task = ctx.store.create_task(
        body.name,
        prompt=prompt,
        model=body.model,
        effort=body.effort,
        schedule=body.schedule.model_dump(),
        project_id=body.project_id,
        enabled=body.enabled,
        next_run_at=next_run(body.schedule, ctx.now_ms()) if body.enabled else None,
    )
    return JSONResponse(task_json(ctx, task), status_code=201)


async def patch_task(request: Request) -> Response:
    ctx = ctx_of(request)
    task = task_or_404(ctx, request.path_params["task_id"])
    body = await read_json(request, EditTask)
    given = body.model_fields_set
    changes: dict[str, object] = {}
    if "name" in given:
        changes["name"] = body.name
    if "prompt" in given:
        changes["prompt"] = checked_prompt(ctx, body.prompt or "")
    if "model" in given:
        check_task_model(ctx, body.model or "")
        changes["model"] = body.model
    if "effort" in given:
        changes["effort"] = body.effort
    if "project_id" in given:
        check_project(ctx, body.project_id)
        changes["project_id"] = body.project_id
    if "schedule" in given:
        assert body.schedule is not None, "a schedule can't be null"
        changes["schedule"] = body.schedule.model_dump()
    enabled = task.enabled if body.enabled is None else body.enabled
    if body.enabled is not None:
        changes["enabled"] = enabled
    # The next run belongs to the schedule: it is set when the task goes on or its schedule changes, and cleared
    # when it goes off. Saving anything else leaves it where it is.
    if not enabled:
        changes["next_run_at"] = None
    elif "schedule" in given or not task.enabled:
        schedule = body.schedule or parse_schedule(task.schedule)
        changes["next_run_at"] = next_run(schedule, ctx.now_ms())
    if enabled and not task.enabled:
        changes["failures"] = 0
        # If the scheduler switched it off, the reason is gone and so is the explanation of it.
        if (task.last_error or "").startswith(SWITCHED_OFF):
            changes.update(
                last_status=None, last_error=None, last_chat_id=None, last_run_at=None
            )
    updated = ctx.store.update_task(task.id, changes)
    return JSONResponse(task_json(ctx, updated) if updated else {})


async def delete_task(request: Request) -> Response:
    ctx = ctx_of(request)
    task = task_or_404(ctx, request.path_params["task_id"])
    ctx.store.delete_task(task.id)
    return Response(status_code=204)


async def run_task_now(request: Request) -> Response:
    ctx = ctx_of(request)
    task = task_or_404(ctx, request.path_params["task_id"])
    outcome = ctx.scheduler.run_now(task)
    if isinstance(outcome, RunResult):
        raise ApiError(
            429 if outcome.status == "busy" else 409,
            "busy" if outcome.status == "busy" else "cannot_run",
            outcome.error,
        )
    return JSONResponse({"chat_id": outcome.chat_id}, status_code=202)


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
    clock: Callable[[], float] = time.time,
    task_tick: float = TICK_SECONDS,
) -> Starlette:
    """`transport`, `providers`, `image_pacing`, `clock` and `task_tick` exist so tests can swap the network and
    the models out, make image jobs look at the engine more often, and wind the scheduler's clock by hand."""

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
        scheduler = Scheduler(store, service, now=clock, tick=task_tick)
        ctx = app.state.ctx = Context(cfg, store, catalog, service, scheduler, clock)
        store.purge_expired(cfg.retention_days)
        await refresh_briefly(catalog, 6.0, force=True)
        task = asyncio.create_task(maintenance(ctx))
        # After the first refresh, so that a task due at start-up sees the models that are there.
        scheduling = asyncio.create_task(scheduler.run_forever())
        try:
            yield
        finally:
            # Replies in progress, scheduled or started by hand, are saved as stopped with what had arrived.
            scheduling.cancel()
            await scheduler.stop()
            if images:
                # Images in the making are saved as stopped; the engine is left to finish and idle out.
                await images.shutdown()
            task.cancel()
            # Both are cancelled before either is awaited: a request that uvicorn has cancelled is still
            # unwinding, and one more turn of the loop here lets it end with a traceback in the log.
            with contextlib.suppress(asyncio.CancelledError):
                await task
            with contextlib.suppress(asyncio.CancelledError):
                await scheduling
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
            Route("/api/chats/{chat_id}", patch_chat, methods=["PATCH"]),
            Route("/api/chats/{chat_id}", delete_chat, methods=["DELETE"]),
            Route("/api/chats/{chat_id}/images/{image_id}", get_image),
            Route("/api/chats/{chat_id}/image/stop", stop_image, methods=["POST"]),
            Route("/api/projects", list_projects),
            Route("/api/projects", create_project, methods=["POST"]),
            Route("/api/projects/{project_id}", patch_project, methods=["PATCH"]),
            Route("/api/projects/{project_id}", delete_project, methods=["DELETE"]),
            Route("/api/tasks", list_tasks),
            Route("/api/tasks", create_task, methods=["POST"]),
            Route("/api/tasks/{task_id}", patch_task, methods=["PATCH"]),
            Route("/api/tasks/{task_id}", delete_task, methods=["DELETE"]),
            Route("/api/tasks/{task_id}/run", run_task_now, methods=["POST"]),
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
