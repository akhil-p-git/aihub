"""One chat turn: validate, store the user's message, stream the reply, store the reply however it ends."""

import asyncio
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .catalog import Catalog, ModelInfo
from .config import Config
from .db import Chat, Message, Project, Store
from .images import (
    IMAGE_STORE_CAP,
    MAX_PROMPT_CHARS,
    SHAPES,
    Busy,
    GpuLease,
    ImageJobs,
    ImagePlan,
    Job,
)
from .providers import (
    Finish,
    GenRequest,
    Provider,
    ProviderError,
    Text,
    Thinking,
    Usage,
)

log = logging.getLogger(__name__)

TITLE_LENGTH = 60
# English averages about four characters per token; assuming three keeps the estimate on the safe side.
CHARS_PER_TOKEN = 3
# Leave a quarter of the context for the reply.
CONTEXT_SHARE = 0.75
TASK_MODEL_MESSAGE = "A scheduled task can only use a text model that runs on this PC."


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str | None = Field(None, pattern=r"^[0-9a-f]{16}$")
    text: str | None = None
    model: str | None = Field(None, max_length=200)
    effort: str | None = Field(None, max_length=16)
    # Answer this user message again. Naming it, not just asking for "again", lets a window that is behind
    # be refused instead of deleting a newer reply.
    regenerate_for: str | None = Field(None, pattern=r"^[0-9a-f]{16}$")
    # The shape of an image; image models only. None means square.
    shape: Literal["square", "wide", "tall"] | None = None
    # The project a new chat goes in. A chat that exists is moved with PATCH /api/chats/{id}, not here.
    project_id: str | None = Field(None, pattern=r"^[0-9a-f]{16}$")

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if self.regenerate_for:
            if not self.chat_id:
                raise ValueError("regenerate_for needs a chat_id")
            if self.text is not None:
                raise ValueError("regenerate_for takes no text")
        elif not (self.text or "").strip():
            raise ValueError("text is empty")
        return self

    @model_validator(mode="after")
    def _only_a_new_chat_takes_a_project(self) -> Self:
        if self.project_id and self.chat_id:
            raise ValueError("project_id only applies to a new chat")
        return self


@dataclass(slots=True)
class Timing:
    """Monotonic timestamps of one reply: when it began, first token, and the thinking span."""

    start: float
    first: float | None = None
    think_from: float | None = None
    think_to: float | None = None
    end: float | None = None


@dataclass(slots=True)
class Turn:
    chat: Chat
    user: Message | None
    model: ModelInfo
    effort: str | None
    request: GenRequest
    dropped: int  # earlier messages left out to fit the context
    # The GPU, shared, for a reply by a local model: an image in the making needs it whole.
    lease: GpuLease | None = None


@dataclass(slots=True)
class ImageTurn:
    """An image to make. Its job is already running: the stream only watches it."""

    chat: Chat
    user: Message | None
    model: ModelInfo
    job: Job


def derive_title(text: str) -> str:
    line = next((ln for ln in text.splitlines() if ln.strip()), "")
    cleaned = " ".join(re.sub(r"[#*`>_\[\]]+", "", line).split())
    if len(cleaned) > TITLE_LENGTH:
        return cleaned[: TITLE_LENGTH - 1].rstrip() + "…"
    return cleaned or "New chat"


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN + 1


def _stand_in(image: Mapping[str, object], names: Mapping[str, str]) -> str:
    name = names.get(str(image["model"]), image["model"])
    return f"[Image made with {name} from the prompt above, {image['w']}x{image['h']}, seed {image['seed']}]"


def build_history(
    messages: list[Message], image_names: Mapping[str, str] | None = None
) -> list[dict[str, str]]:
    """Alternating turns for the provider.

    Failed and empty replies are skipped, so a retry never resends them, and adjacent turns by the same
    role are merged because providers want strict alternation. A reply that is an image is described in
    words, with the name of the model that made it (`image_names` maps model ids to names): text models
    are told an image was made, never shown it.
    """
    names = image_names or {}
    turns: list[dict[str, str]] = []
    for message in messages:
        content = message.content
        if message.role == "assistant" and message.images:
            content = "\n".join(
                filter(
                    None,
                    [content.strip(), *(_stand_in(i, names) for i in message.images)],
                )
            )
        elif message.role == "assistant" and (
            message.status == "error" or not message.content.strip()
        ):
            continue
        if turns and turns[-1]["role"] == message.role:
            turns[-1]["content"] += "\n\n" + content
        else:
            turns.append({"role": message.role, "content": content})
    while turns and turns[0]["role"] != "user":
        turns.pop(0)
    return turns


def fit_history(
    turns: list[dict[str, str]], budget: int
) -> tuple[list[dict[str, str]], int]:
    """Drop the oldest turns until the estimate fits. The newest turn always stays."""
    kept = list(turns)
    total = sum(estimate_tokens(t["content"]) for t in kept)
    while len(kept) > 1 and (total > budget or kept[0]["role"] != "user"):
        total -= estimate_tokens(kept.pop(0)["content"])
    return kept, len(turns) - len(kept)


def system_prompt(
    instructions: str, today: date, project: Project | None = None
) -> str:
    """The date, then the user's standing instructions, then those of the chat's project. All three are the
    user's own words, so all are trusted the same."""
    parts = [f"The current date is {today.isoformat()}."]
    if instructions.strip():
        parts.append("Standing instructions from the user:\n" + instructions.strip())
    if project and project.instructions.strip():
        parts.append(
            f'Instructions for the project "{project.name}":\n'
            + project.instructions.strip()
        )
    return "\n\n".join(parts)


def max_tokens_for(model: ModelInfo, effort: str | None, cfg: Config) -> int:
    if model.provider == "ollama":
        return cfg.ollama.num_predict
    if model.provider == "openrouter":
        return cfg.openrouter.max_tokens
    deep = effort in ("xhigh", "max")
    return cfg.anthropic.max_tokens_deep if deep else cfg.anthropic.max_tokens


def cost_of(model: ModelInfo, usage: Usage | None) -> Decimal | None:
    if usage is None:
        return None
    if usage.cost is not None:
        return usage.cost
    if None in (
        model.price_in,
        model.price_out,
        usage.input_tokens,
        usage.output_tokens,
    ):
        return None
    total = (
        Decimal(usage.input_tokens) * model.price_in
        + Decimal(usage.output_tokens) * model.price_out
    )
    return total / 1_000_000


def _money(value: Decimal) -> str:
    return format(value.normalize(), "f")


class ChatService:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        catalog: Catalog,
        providers: Mapping[str, Provider],
        *,
        today: Callable[[], date] = date.today,
        monotonic: Callable[[], float] = time.monotonic,
        images: ImageJobs | None = None,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._catalog = catalog
        self._providers = providers
        self._today = today
        self._monotonic = monotonic
        self._gate = asyncio.Semaphore(cfg.limits.max_concurrent)
        # Set where image generation is: the jobs, and with them the GPU lock.
        self.images = images

    def prepare(
        self,
        body: ChatRequest,
        *,
        title: str | None = None,
        task_id: str | None = None,
    ) -> Turn | ImageTurn:
        """Everything that can be refused is refused here, before any bytes are streamed.

        `title` and `task_id` are for the run of a scheduled task, which makes a new chat named for the task and
        linked to it. A browser cannot send them. Nobody watches a task, so it never spends money or sends its
        prompt away: only a text model on this PC is accepted for one.
        """
        chat = self._store.get_chat(body.chat_id) if body.chat_id else None
        if body.chat_id and not chat:
            raise ApiError(404, "no_chat", "That chat no longer exists.")
        if body.project_id and self._store.get_project(body.project_id) is None:
            raise ApiError(404, "no_project", "That project no longer exists.")
        model = self._resolve_model(body.model or (chat.model if chat else None))
        if task_id and model.provider != "ollama":
            raise ApiError(422, "task_model", TASK_MODEL_MESSAGE)
        if model.output == "image":
            return self._prepare_image(body, chat, model)
        if body.shape:
            raise ApiError(422, "invalid", "shape/reference only apply to image models")
        lease = self._share_the_gpu(model)
        try:
            turn = self._prepare_text(body, chat, model, title, task_id)
        except BaseException:
            if lease:
                lease.release()
            raise
        turn.lease = lease
        return turn

    def _share_the_gpu(self, model: ModelInfo) -> GpuLease | None:
        """A local reply takes part of the GPU, which an image in the making needs whole, so it is refused
        while one is made. Cloud models never need it."""
        if self.images is None or model.provider != "ollama":
            return None
        try:
            return self.images.gpu.share()
        except Busy as err:
            raise ApiError(429, "busy_gpu", str(err)) from err

    def _prepare_text(
        self,
        body: ChatRequest,
        chat: Chat | None,
        model: ModelInfo,
        title: str | None,
        task_id: str | None,
    ) -> Turn:
        effort = self._resolve_effort(
            model, body.effort or (chat.effort if chat else None)
        )
        if self._gate.locked():
            raise ApiError(
                429,
                "busy",
                "Several replies are already streaming. Wait for one to finish.",
            )

        user = None
        if body.regenerate_for:
            self._drop_last_reply(chat, body.regenerate_for)
        else:
            text = body.text or ""
            if len(text) > self._cfg.limits.max_message_chars:
                raise ApiError(
                    413,
                    "too_long",
                    f"Messages are limited to {self._cfg.limits.max_message_chars:,} characters.",
                )
            if not chat:
                chat = self._store.create_chat(
                    title or derive_title(text),
                    model.id,
                    effort,
                    body.project_id,
                    task_id,
                )
            user = self._store.add_message(chat.id, "user", text)
        if (chat.model, chat.effort) != (model.id, effort):
            chat = (
                self._store.update_chat(chat.id, {"model": model.id, "effort": effort})
                or chat
            )

        project = self._store.get_project(chat.project_id) if chat.project_id else None
        system = system_prompt(
            self._store.get_setting("instructions"), self._today(), project
        )
        turns, dropped = fit_history(
            build_history(self._store.messages(chat.id), self._image_names()),
            # The budget is for the whole request: what the system prompt takes, the history cannot.
            self._budget(model) - estimate_tokens(system),
        )
        if not turns:
            raise ApiError(409, "nothing_to_answer", "There is no message to answer.")
        request = GenRequest(
            model, turns, system, effort, max_tokens_for(model, effort, self._cfg)
        )
        return Turn(chat, user, model, effort, request, dropped)

    def _resolve_model(self, model_id: str | None) -> ModelInfo:
        model_id = model_id or self._catalog.default_model()
        if model_id is None:
            raise ApiError(
                409,
                "no_model",
                "No model is available. Start Ollama or add an OpenRouter key.",
            )
        model = self._catalog.get(model_id)
        if model is None:
            raise ApiError(
                400, "unknown_model", f"There is no model called {model_id}."
            )
        if not model.available:
            raise ApiError(
                409,
                "model_unavailable",
                f"{model.name} isn't available: {(model.reason or '').rstrip('.')}.",
            )
        return model

    def _image_names(self) -> dict[str, str]:
        return {f"images/{m.id}": m.name for m in self._cfg.images.models}

    @staticmethod
    def _resolve_effort(model: ModelInfo, effort: str | None) -> str | None:
        """A stale or unsupported level (say, after switching models) falls back to the model's default."""
        if not model.efforts:
            return None
        return effort if effort in model.efforts.levels else model.efforts.default

    def _drop_last_reply(self, chat: Chat | None, user_id: str) -> None:
        """Deletes the reply to the newest user message, which must be the one the caller means."""
        messages = self._store.messages(chat.id) if chat else []
        newest = next((m for m in reversed(messages) if m.role == "user"), None)
        if newest is None:
            raise ApiError(409, "nothing_to_answer", "There is no message to answer.")
        if newest.id != user_id:
            raise ApiError(
                409,
                "stale_chat",
                "This chat changed in another window.",
            )
        if messages[-1].role == "assistant":
            self._store.delete_message(messages[-1].id)

    def _prepare_image(
        self, body: ChatRequest, chat: Chat | None, model: ModelInfo
    ) -> ImageTurn:
        """Check everything, store the prompt and start the job, which runs on without the request."""
        jobs = self.images
        assert jobs is not None, "an image model is only listed where images are set up"
        if body.regenerate_for:
            newest = next(
                (
                    m
                    for m in reversed(self._store.messages(chat.id))
                    if m.role == "user"
                ),
                None,
            )
            if newest is None:
                raise ApiError(
                    409, "nothing_to_answer", "There is no message to answer."
                )
            prompt = newest.content.strip()
        else:
            prompt = (body.text or "").strip()
        if len(prompt) > MAX_PROMPT_CHARS:
            raise ApiError(
                413,
                "prompt_too_long",
                f"Image prompts are limited to {MAX_PROMPT_CHARS:,} characters.",
            )
        if self._store.image_bytes_total() >= IMAGE_STORE_CAP:
            raise ApiError(
                507,
                "image_store_full",
                "Image storage is full (2 GiB). Delete old chats to make room.",
            )
        try:
            jobs.admit(chat.id if chat else None)
        except Busy as err:
            raise ApiError(429, "busy_gpu", str(err)) from err

        user = None
        if body.regenerate_for:
            self._drop_last_reply(chat, body.regenerate_for)
        else:
            if not chat:
                chat = self._store.create_chat(
                    derive_title(prompt), model.id, None, body.project_id
                )
            user = self._store.add_message(chat.id, "user", prompt)
        if (chat.model, chat.effort) != (model.id, None):
            chat = (
                self._store.update_chat(chat.id, {"model": model.id, "effort": None})
                or chat
            )
        shape = body.shape or "square"
        config = next(m for m in self._cfg.images.models if m.id == model.native)
        plan = ImagePlan(
            chat.id,
            model.id,
            model.native,
            prompt,
            shape,
            *SHAPES[shape],
            secrets.randbelow(2**31),
            config.steps,
            config.sample_method,
            getattr(config.est_s, shape),
        )
        return ImageTurn(chat, user, model, jobs.start(plan))

    def _budget(self, model: ModelInfo) -> int:
        usable = int((model.ctx or 8192) * CONTEXT_SHARE)
        if model.provider == "ollama":
            return usable
        return min(usable, self._cfg.limits.cloud_context_tokens)

    def stream(self, turn: Turn | ImageTurn) -> AsyncIterator[dict[str, object]]:
        if isinstance(turn, ImageTurn):
            return self._stream_image(turn)
        return self._stream_text(turn)

    async def _stream_image(self, turn: ImageTurn) -> AsyncIterator[dict[str, object]]:
        """Events for the browser: the chat, then the job's progress, then how it ended.

        The job owns the work. Leaving here, however that happens, only stops the listening, so nothing
        in this method may await while it is being closed.
        """
        queue = turn.job.subscribe()
        try:
            yield {
                "t": "chat",
                "chat": turn.chat.to_json(),
                "user": turn.user.to_json() if turn.user else None,
            }
            while True:
                event = await queue.get()
                yield event
                if event["t"] != "progress":
                    return
        finally:
            turn.job.unsubscribe(queue)

    async def _stream_text(self, turn: Turn) -> AsyncIterator[dict[str, object]]:
        """Events for the browser. The reply is stored exactly once, whether it finishes, fails or is cut off."""
        text: list[str] = []
        thinking: list[str] = []
        usage: Usage | None = None
        finish: str | None = None
        status, error = "done", None
        timing = Timing(self._monotonic())
        try:
            yield {
                "t": "chat",
                "chat": turn.chat.to_json(),
                "user": turn.user.to_json() if turn.user else None,
            }
            if turn.dropped:
                yield {"t": "note", "dropped": turn.dropped}
            async with self._gate:
                async for event in self._providers[turn.model.provider].stream(
                    turn.request
                ):
                    now = self._monotonic()
                    if isinstance(event, Thinking):
                        timing.first = timing.first if timing.first is not None else now
                        timing.think_from = (
                            timing.think_from if timing.think_from is not None else now
                        )
                        thinking.append(event.text)
                        yield {"t": "think", "d": event.text}
                    elif isinstance(event, Text):
                        timing.first = timing.first if timing.first is not None else now
                        if timing.think_from is not None and timing.think_to is None:
                            timing.think_to = now
                        text.append(event.text)
                        yield {"t": "text", "d": event.text}
                    elif isinstance(event, Usage):
                        usage = event
                    elif isinstance(event, Finish):
                        finish = event.reason
        except ProviderError as err:
            status, error = "error", {"code": err.code, "message": err.message}
        except Exception:
            log.exception("reply failed in chat %s", turn.chat.id)
            status = "error"
            error = {
                "code": "internal",
                "message": "The server hit an unexpected error. The service log has the details.",
            }
        except BaseException:
            status = "stopped"  # the reader went away or pressed Stop
            raise
        finally:
            if turn.lease:
                turn.lease.release()
            timing.end = self._monotonic()
            if timing.think_from is not None and timing.think_to is None:
                timing.think_to = timing.end
            message = self._save(
                turn,
                "".join(text),
                "".join(thinking) or None,
                status,
                error,
                self._usage_json(turn.model, usage, finish, timing),
            )
        yield {"t": "error" if error else "done", "message": message.to_json()}

    def _usage_json(
        self, model: ModelInfo, usage: Usage | None, finish: str | None, timing: Timing
    ) -> dict[str, object]:
        end = timing.end if timing.end is not None else timing.start
        data: dict[str, object] = {"total_ms": round((end - timing.start) * 1000)}
        if timing.first is not None:
            data["ttft_ms"] = round((timing.first - timing.start) * 1000)
        if timing.think_from is not None and timing.think_to is not None:
            data["think_ms"] = round((timing.think_to - timing.think_from) * 1000)
        if finish:
            data["finish"] = finish
        if usage:
            data["in"], data["out"] = usage.input_tokens, usage.output_tokens
            speed = usage.tokens_per_second
            streaming_for = end - timing.first if timing.first is not None else 0
            if speed is None and usage.output_tokens and streaming_for > 0.05:
                speed = usage.output_tokens / streaming_for
            if speed:
                data["tps"] = round(speed, 1)
            cost = cost_of(model, usage)
            if cost is not None:
                data["cost"] = _money(cost)
        return {k: v for k, v in data.items() if v is not None}

    def _save(
        self,
        turn: Turn,
        content: str,
        thinking: str | None,
        status: str,
        error: dict[str, str] | None,
        usage: dict[str, object] | None,
    ) -> Message:
        try:
            message = self._store.add_message(
                turn.chat.id,
                "assistant",
                content,
                thinking=thinking,
                model=turn.model.id,
                effort=turn.effort,
                usage=usage,
                status=status,
                error=error,
            )
        except sqlite3.IntegrityError:
            # The chat was deleted while the reply was streaming; there is nowhere to store it.
            log.info("chat %s vanished during its reply", turn.chat.id)
            message = Message(
                "",
                turn.chat.id,
                0,
                "assistant",
                content,
                thinking,
                turn.model.id,
                turn.effort,
                usage,
                status,
                error,
                0,
            )
        log.info(
            "reply chat=%s model=%s effort=%s status=%s %s",
            turn.chat.id,
            turn.model.id,
            turn.effort,
            status,
            " ".join(f"{k}={v}" for k, v in (usage or {}).items()),
        )
        return message
