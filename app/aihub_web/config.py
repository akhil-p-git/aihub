"""Settings for the web app: a YAML file for options, environment variables for secrets."""

import os
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = Path("~/.config/aihub/web.yaml")
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class ConfigError(Exception):
    """The settings file is unusable; the message lists every problem."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class OllamaConfig(_Section):
    url: str = "http://127.0.0.1:11434"
    # Sent with every request: Ollama reloads a model when num_ctx changes, so keep it constant.
    num_ctx: int = Field(8192, ge=2048, le=262_144)
    num_predict: int = Field(8192, ge=256)
    keep_alive: str = "10m"


class OpenRouterConfig(_Section):
    url: str = "https://openrouter.ai/api/v1"
    key_env: str = "AIHUB_OPENROUTER_KEY"
    paid_models: tuple[str, ...] = (
        "deepseek/deepseek-v4.1-flash-1017",
        "deepseek/deepseek-v4-pro-0813",
    )
    # Routing for paid models only: free models can't satisfy a "deny data collection" policy.
    paid_provider: dict[str, object] = Field(
        default_factory=lambda: {
            "data_collection": "deny",
            "ignore": ["deepseek", "baidu", "streamlake", "siliconflow", "alibaba"],
        }
    )
    max_tokens: int = Field(8192, ge=256)


class ClaudeModel(_Section):
    id: str
    name: str
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    ctx: int = 1_000_000
    # How "off" is requested (`thinking: {type: ...}`); None means thinking can't be turned off.
    thinking_off: Literal["disabled", "between_tools"] | None = None


DEFAULT_CLAUDE = (
    ClaudeModel(
        id="claude-haiku-5-5",
        name="Haiku 5.5",
        input_per_mtok=Decimal("0.10"),
        output_per_mtok=Decimal("0.50"),
        thinking_off="disabled",
    ),
    ClaudeModel(
        id="claude-sonnet-5-5",
        name="Sonnet 5.5",
        input_per_mtok=Decimal(2),
        output_per_mtok=Decimal(10),
        thinking_off="between_tools",
    ),
    ClaudeModel(
        id="claude-opus-5-5",
        name="Opus 5.5",
        input_per_mtok=Decimal(4),
        output_per_mtok=Decimal(20),
    ),
)


class AnthropicConfig(_Section):
    url: str = "https://api.anthropic.com"
    key_env: str = "AIHUB_ANTHROPIC_KEY"
    models: tuple[ClaudeModel, ...] = DEFAULT_CLAUDE
    max_tokens: int = Field(16_000, ge=1024)
    # xhigh and max effort think for longer, and thinking counts toward max_tokens.
    max_tokens_deep: int = Field(64_000, ge=1024)


class ImageEstimates(_Section):
    """Seconds one image takes on a warm engine, per shape: the picker's hint, and (with 3x and a minute) the job's time limit."""

    square: int | float = Field(gt=0)
    wide: int | float = Field(gt=0)
    tall: int | float = Field(gt=0)


class ImageModel(_Section):
    # Also a path component for the engine launcher, hence the strict pattern.
    id: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    name: str
    steps: int = Field(ge=1)
    sample_method: str | None = None
    edits: bool = False
    note: str = ""
    est_s: ImageEstimates


DEFAULT_IMAGE_MODELS = (
    ImageModel(
        id="zimage",
        name="Z-Image Turbo",
        steps=8,
        note="Best with text in images.",
        est_s=ImageEstimates(square=23, wide=12, tall=12),
    ),
    ImageModel(
        id="klein4b",
        name="FLUX.2 klein 4B",
        steps=4,
        sample_method="euler",
        edits=True,
        note="Fast drafts; edits images; text often misspelled.",
        est_s=ImageEstimates(square=8, wide=5, tall=5),
    ),
)


class ImagesConfig(_Section):
    # The image engine's Unix socket. Unset means there is no image feature at all.
    socket: str | None = None
    # The first model is the default image model.
    models: tuple[ImageModel, ...] = DEFAULT_IMAGE_MODELS

    @field_validator("socket")
    @classmethod
    def _absolute(cls, value: str | None) -> str | None:
        if value is not None and not Path(value).is_absolute():
            raise ValueError(f"must be an absolute path, got {value!r}")
        return value

    @model_validator(mode="after")
    def _unique(self) -> Self:
        ids = [model.id for model in self.models]
        if len(set(ids)) != len(ids):
            raise ValueError(f"model ids must be unique, got {ids}")
        return self


class Limits(_Section):
    max_message_chars: int = Field(100_000, ge=1)
    # History sent to cloud models is trimmed to this many (estimated) tokens to bound the cost of a turn.
    cloud_context_tokens: int = Field(60_000, ge=1000)
    max_concurrent: int = Field(3, ge=1)


class Config(_Section):
    host: str = "127.0.0.1"
    # 0 lets the system pick a free port.
    port: int = Field(8787, ge=0, le=65535)
    allowed_hosts: tuple[str, ...] = ("127.0.0.1", "localhost")
    state_dir: Path = REPO_ROOT / "state" / "web"
    retention_days: int = Field(30, ge=1)
    # The first available entry is the model every new chat starts with. Free or local only.
    default_models: tuple[str, ...] = (
        "openrouter/google/gemma-4-31b-it:free",
        "ollama/qwen3:8b",
    )
    ollama: OllamaConfig = Field(default_factory=OllamaConfig)
    openrouter: OpenRouterConfig = Field(default_factory=OpenRouterConfig)
    anthropic: AnthropicConfig = Field(default_factory=AnthropicConfig)
    images: ImagesConfig = Field(default_factory=ImagesConfig)
    limits: Limits = Field(default_factory=Limits)

    @field_validator("state_dir")
    @classmethod
    def _expand_home(cls, value: Path) -> Path:
        return value.expanduser()

    @model_validator(mode="after")
    def _check(self) -> Self:
        problems = []
        if self.host not in LOOPBACK_HOSTS:
            problems.append(
                f"host must be a loopback address (got {self.host!r}); use `tailscale serve` to reach it from other devices"
            )
        if not self.default_models:
            problems.append("default_models is empty")
        for model in self.default_models:
            if model.startswith("images/"):
                problems.append(
                    f"default_models can't include an image model, got {model!r}"
                )
                continue
            is_local = model.startswith("ollama/")
            is_free = model.startswith("openrouter/") and model.endswith(":free")
            if not (is_local or is_free):
                problems.append(
                    f"default_models must be local or free models, got {model!r}"
                )
        if problems:
            raise ValueError("; ".join(problems))
        return self


def load_config(
    path: Path | None = None, environ: Mapping[str, str] = os.environ
) -> Config:
    """Read the YAML settings (defaults when the file is absent).

    AIHUB_STATE_DIR overrides state_dir and AIHUB_PORT overrides port.
    """
    path = path or Path(environ.get("AIHUB_WEB_CONFIG") or DEFAULT_CONFIG_PATH)
    path = path.expanduser()
    raw: dict[str, object] = {}
    if path.exists():
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ConfigError(f"{path}: expected a mapping at the top level")
        raw = loaded
    if state_dir := environ.get("AIHUB_STATE_DIR"):
        raw["state_dir"] = state_dir
    if port := environ.get("AIHUB_PORT"):
        raw["port"] = port
    try:
        return Config.model_validate(raw)
    except ValidationError as err:
        problems = [
            f"{'.'.join(str(part) for part in e['loc']) or '<root>'}: {e['msg']}"
            for e in err.errors()
        ]
        raise ConfigError(f"{path}: " + "; ".join(problems)) from err


def secret(name: str, environ: Mapping[str, str] = os.environ) -> str | None:
    """The value of an API key variable, or None when it is unset or blank."""
    return environ.get(name, "").strip() or None
