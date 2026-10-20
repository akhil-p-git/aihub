"""The models the picker offers: installed Ollama models, OpenRouter's free list and pinned paid models, and Claude."""

import asyncio
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Self

import httpx2

from .config import ClaudeModel, Config, OllamaConfig, secret

log = logging.getLogger(__name__)

# Effort levels from least to most, as the UI shows them. "off" is reasoning disabled.
LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
CLAUDE_LEVELS = ("low", "medium", "high", "xhigh", "max")
OLLAMA_TTL = 15.0
OPENROUTER_TTL = 6 * 3600.0
NOT_CHAT_MODELS = ("content-safety", "guard")


@dataclass(frozen=True, slots=True)
class Effort:
    levels: tuple[str, ...]
    default: str

    @classmethod
    def of(cls, levels: tuple[str, ...]) -> Self:
        return cls(levels, default_level(levels))


def default_level(levels: tuple[str, ...]) -> str:
    """New chats start at the cheapest setting that still reasons, else with reasoning off."""
    for preferred in ("low", "off"):
        if preferred in levels:
            return preferred
    return levels[0]


@dataclass(frozen=True, slots=True)
class ModelInfo:
    id: str  # "<provider>/<native id>", e.g. "ollama/qwen3:8b"
    provider: str
    native: str
    name: str
    tier: str  # "free", "local" or "paid"
    ctx: int | None = None  # tokens we will actually use
    ctx_max: int | None = None  # tokens the model supports
    vision: bool = False
    efforts: Effort | None = None
    price_in: Decimal | None = None  # USD per million tokens
    price_out: Decimal | None = None
    expires: str | None = None  # ISO date after which the model disappears
    size_bytes: int | None = None
    available: bool = True
    reason: str | None = None

    def to_json(self) -> dict[str, object]:
        price = None
        if self.price_in is not None and self.price_out is not None:
            price = {"in": _plain(self.price_in), "out": _plain(self.price_out)}
        return {
            "id": self.id,
            "provider": self.provider,
            "name": self.name,
            "tier": self.tier,
            "ctx": self.ctx,
            "vision": self.vision,
            "efforts": {"levels": self.efforts.levels, "default": self.efforts.default}
            if self.efforts
            else None,
            "price": price,
            "expires": self.expires,
            "size_bytes": self.size_bytes,
            "available": self.available,
            "reason": self.reason,
        }


def _plain(value: Decimal) -> str:
    return format(value.normalize(), "f")


def ollama_efforts(show: Mapping[str, object]) -> Effort | None:
    """Reasoning control from /api/show: only the values the model lists can be sent.

    No `thinking` object means the model always thinks (deepseek-r1:8b ignores think=false), and
    `[true]` alone means the same; a control needs at least two choices.
    """
    if "thinking" not in (show.get("capabilities") or []):
        return None
    spec = show.get("thinking")
    values = spec.get("values") if isinstance(spec, dict) else None
    if not isinstance(values, list):
        return None
    levels = [v for v in values if isinstance(v, str)]
    if not levels and any(v is True for v in values):
        levels = ["on"]
    if any(v is False for v in values):
        levels.insert(0, "off")
    return Effort.of(tuple(levels)) if len(levels) > 1 else None


def openrouter_efforts(reasoning: Mapping[str, object] | None) -> Effort | None:
    """Reasoning control from the model's `reasoning` object; "none" is OpenRouter's name for off."""
    if not reasoning:
        return None
    if "supported_efforts" not in reasoning:
        return None  # the model does not expose effort selection
    supported = reasoning["supported_efforts"]
    if (
        supported is None
    ):  # documented as: every effort level the gateway knows is accepted
        supported = [("none" if level == "off" else level) for level in LEVELS]
    levels = {"off" if e == "none" else e for e in supported}
    if reasoning.get("mandatory"):
        levels.discard("off")
    ordered = tuple(level for level in LEVELS if level in levels)
    return Effort.of(ordered) if len(ordered) > 1 else None


def claude_model(model: ClaudeModel) -> ModelInfo:
    levels = (("off",) if model.thinking_off else ()) + CLAUDE_LEVELS
    return ModelInfo(
        id=f"anthropic/{model.id}",
        provider="anthropic",
        native=model.id,
        name=model.name,
        tier="paid",
        ctx=model.ctx,
        ctx_max=model.ctx,
        vision=True,
        efforts=Effort.of(levels),
        price_in=model.input_per_mtok,
        price_out=model.output_per_mtok,
    )


def ollama_model(
    tag: Mapping[str, object], show: Mapping[str, object], cfg: OllamaConfig
) -> ModelInfo | None:
    capabilities = show.get("capabilities") or []
    if "completion" not in capabilities:  # embedding-only models can't chat
        return None
    name = str(tag["name"])
    info = show.get("model_info") or {}
    declared = next(
        (
            v
            for k, v in info.items()
            if k.endswith(".context_length") and isinstance(v, int)
        ),
        None,
    )
    size = tag.get("size")
    return ModelInfo(
        id=f"ollama/{name}",
        provider="ollama",
        native=name,
        name=name,
        tier="local",
        ctx=min(declared, cfg.num_ctx) if declared else cfg.num_ctx,
        ctx_max=declared,
        vision="vision" in capabilities,
        efforts=ollama_efforts(show),
        size_bytes=size if isinstance(size, int) else None,
    )


def _per_mtok(value: object) -> Decimal | None:
    try:
        price = Decimal(str(value)) * 1_000_000
    except InvalidOperation:
        return None
    return price if price >= 0 else None  # negative means "priced dynamically"


def _parse_date(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def openrouter_model(raw: Mapping[str, object], today: date) -> ModelInfo | None:
    model_id = str(raw["id"])
    free = model_id.endswith(":free")
    architecture = raw.get("architecture") or {}
    if "text" not in (architecture.get("output_modalities") or ["text"]):
        return None
    if free and any(word in model_id for word in NOT_CHAT_MODELS):
        return None
    expires = _parse_date(raw.get("expiration_date"))
    if expires and expires < today:
        return None
    pricing = raw.get("pricing") or {}
    context = raw.get("context_length")
    name = str(raw.get("name") or model_id).removesuffix(" (free)")
    return ModelInfo(
        id=f"openrouter/{model_id}",
        provider="openrouter",
        native=model_id,
        name=name,
        tier="free" if free else "paid",
        ctx=context if isinstance(context, int) else None,
        ctx_max=context if isinstance(context, int) else None,
        vision="image" in (architecture.get("input_modalities") or []),
        efforts=openrouter_efforts(raw.get("reasoning")),
        price_in=None if free else _per_mtok(pricing.get("prompt")),
        price_out=None if free else _per_mtok(pricing.get("completion")),
        expires=expires.isoformat() if expires else None,
    )


class Catalog:
    def __init__(
        self,
        cfg: Config,
        client: httpx2.AsyncClient,
        *,
        environ: Mapping[str, str] = os.environ,
        today: Callable[[], date] = date.today,
    ) -> None:
        self._cfg = cfg
        self._client = client
        self._environ = environ
        self._today = today
        self._cache_path = cfg.state_dir / "openrouter-models.json"
        self._shows: dict[str, tuple[str, ModelInfo | None]] = {}
        self._openrouter_raw: list[dict[str, object]] = []
        self._ollama_at = 0.0
        self._openrouter_at = 0.0
        self._claude = [claude_model(m) for m in cfg.anthropic.models]
        self.status: dict[str, dict[str, object]] = {
            name: {"ok": None, "error": None}
            for name in ("ollama", "openrouter", "anthropic")
        }

    async def refresh(self, *, force: bool = False) -> None:
        """Re-read what is stale. Failures are recorded in `status`, never raised."""
        now = time.monotonic()
        async with asyncio.TaskGroup() as group:
            if force or now - self._ollama_at > OLLAMA_TTL:
                group.create_task(self._refresh_ollama())
            if force or now - self._openrouter_at > OPENROUTER_TTL:
                group.create_task(self._refresh_openrouter())

    async def _refresh_ollama(self) -> None:
        url = self._cfg.ollama.url
        try:
            response = await self._client.get(f"{url}/api/tags", timeout=3.0)
            response.raise_for_status()
            tags = response.json().get("models", [])
            gate = asyncio.Semaphore(4)

            async def show(tag: dict[str, object]) -> None:
                async with gate:
                    reply = await self._client.post(
                        f"{url}/api/show", json={"model": tag["name"]}, timeout=10.0
                    )
                    reply.raise_for_status()
                    info = ollama_model(tag, reply.json(), self._cfg.ollama)
                    self._shows[str(tag["name"])] = (str(tag.get("digest", "")), info)

            async with asyncio.TaskGroup() as group:
                for tag in tags:
                    cached = self._shows.get(str(tag["name"]))
                    if not cached or cached[0] != str(tag.get("digest", "")):
                        group.create_task(show(tag))
            names = {str(tag["name"]) for tag in tags}
            self._shows = {k: v for k, v in self._shows.items() if k in names}
        except (httpx2.HTTPError, ValueError, KeyError, ExceptionGroup) as err:
            self.status["ollama"] = {"ok": False, "error": _describe(err)}
        else:
            self.status["ollama"] = {"ok": True, "error": None}
        self._ollama_at = time.monotonic()

    async def _refresh_openrouter(self) -> None:
        url = self._cfg.openrouter.url
        wanted = set(self._cfg.openrouter.paid_models)
        try:
            response = await self._client.get(f"{url}/models", timeout=15.0)
            response.raise_for_status()
            records = response.json()["data"]
            self._openrouter_raw = [
                r
                for r in records
                if str(r.get("id", "")).endswith(":free") or r.get("id") in wanted
            ]
            self._write_cache()
        except (httpx2.HTTPError, ValueError, KeyError, TypeError) as err:
            self.status["openrouter"] = {"ok": False, "error": _describe(err)}
            if not self._openrouter_raw:
                self._openrouter_raw = self._read_cache()
        else:
            self.status["openrouter"] = {"ok": True, "error": None}
        self._openrouter_at = time.monotonic()

    def _write_cache(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temp = self._cache_path.with_suffix(".tmp")
        fd = os.open(temp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(self._openrouter_raw, handle)
        temp.replace(self._cache_path)

    def _read_cache(self) -> list[dict[str, object]]:
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def models(self) -> list[ModelInfo]:
        """Everything the picker lists, free first, with availability decided by which keys are set."""
        today = self._today()
        parsed = {}
        for raw in self._openrouter_raw:
            try:
                info = openrouter_model(raw, today)
            except (KeyError, TypeError, ValueError):
                continue
            if info:
                parsed[info.native] = info
        free = sorted(
            (m for m in parsed.values() if m.tier == "free"),
            key=lambda m: m.name.lower(),
        )
        local = sorted(
            (info for _, info in self._shows.values() if info),
            key=lambda m: m.name.lower(),
        )
        # Pinned paid models still show (without prices) when OpenRouter's list can't be fetched.
        paid_or = [
            parsed.get(pid)
            or ModelInfo(
                id=f"openrouter/{pid}",
                provider="openrouter",
                native=pid,
                name=pid,
                tier="paid",
            )
            for pid in self._cfg.openrouter.paid_models
        ]
        routes = {
            "openrouter": (self._cfg.openrouter.key_env, "OpenRouter"),
            "anthropic": (self._cfg.anthropic.key_env, "Anthropic"),
        }
        ollama_down = self.status["ollama"]["ok"] is False
        out = []
        for model in [*free, *local, *paid_or, *self._claude]:
            route = routes.get(model.provider)
            if model.provider == "ollama" and ollama_down:
                model = replace(
                    model, available=False, reason="Ollama isn't responding"
                )
            elif route and not secret(route[0], self._environ):
                model = replace(
                    model,
                    available=False,
                    reason=f"Set {route[0]} in ~/.config/aihub/aihub.env and restart the service",
                )
            out.append(model)
        return out

    def get(self, model_id: str) -> ModelInfo | None:
        return next((m for m in self.models() if m.id == model_id), None)

    def default_model(self) -> str | None:
        """The first configured default that is usable, else any usable free or local model."""
        usable = {m.id: m for m in self.models() if m.available}
        for model_id in self._cfg.default_models:
            if model_id in usable:
                return model_id
        return next((m.id for m in usable.values() if m.tier != "paid"), None)


def _describe(err: BaseException) -> str:
    if isinstance(err, ExceptionGroup):
        err = err.exceptions[0]
    return f"{type(err).__name__}: {err}"[:200]
