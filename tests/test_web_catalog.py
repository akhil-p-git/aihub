import json
import stat
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx2
import pytest
from aihub_web.catalog import (
    Catalog,
    claude_model,
    default_level,
    ollama_efforts,
    ollama_model,
    openrouter_efforts,
    openrouter_model,
)
from aihub_web.config import Config, OllamaConfig
from helpers import KEYS, OLLAMA, OPENROUTER, TODAY, handler_for


def record(model_id: str) -> dict:
    return next(r for r in OPENROUTER if r["id"] == model_id)


class TestEfforts:
    @pytest.mark.parametrize(
        ("name", "levels"),
        [
            ("qwen3:8b", ("off", "on")),
            ("gemma4:e4b", ("off", "on")),
            ("qwen3:4b", None),  # values [true]: always thinks
            (
                "deepseek-r1:8b",
                None,
            ),  # no thinking object: always thinks, ignores think=false
            ("lfm2.5:8b", None),  # values [false]: never thinks
            ("llama3.2:1b", None),
        ],
    )
    def test_ollama_models_on_this_machine(self, name: str, levels) -> None:
        effort = ollama_efforts(OLLAMA["show"][name])
        assert (effort.levels if effort else None) == levels

    def test_ollama_named_levels(self) -> None:
        show = {
            "capabilities": ["thinking"],
            "thinking": {"values": ["low", "medium", "high"]},
        }
        assert ollama_efforts(show).levels == ("low", "medium", "high")
        show["thinking"]["values"] = [False, "low", "high"]
        assert ollama_efforts(show).levels == ("off", "low", "high")

    @pytest.mark.parametrize(
        ("model_id", "levels"),
        [
            ("google/gemma-4-31b-it:free", None),
            ("liquid/lfm-2.5-2.6b:free", None),  # mandatory, no levels to choose
            ("nvidia/nemotron-3-super-120b-a12b:free", ("off", "low", "medium")),
            (
                "minimax/minimax-m3.1-flash:free",
                ("low", "medium", "high", "xhigh", "max"),
            ),
            ("deepseek/deepseek-v4.1-flash-1017", ("off", "low", "high", "max")),
        ],
    )
    def test_openrouter_models(self, model_id: str, levels) -> None:
        effort = openrouter_efforts(record(model_id)["reasoning"])
        assert (effort.levels if effort else None) == levels

    def test_mandatory_reasoning_never_offers_off(self) -> None:
        reasoning = {"mandatory": True, "supported_efforts": ["none", "low", "high"]}
        assert openrouter_efforts(reasoning).levels == ("low", "high")

    @pytest.mark.parametrize(
        ("levels", "expected"),
        [
            (("off", "low", "high"), "low"),
            (("off", "on"), "off"),
            (("medium", "high"), "medium"),
            (("minimal", "high"), "minimal"),
        ],
    )
    def test_new_chats_start_cheap(self, levels, expected: str) -> None:
        assert default_level(levels) == expected

    def test_only_models_that_can_turn_thinking_off_offer_off(self) -> None:
        cfg = Config().anthropic
        by_name = {m.name: claude_model(m) for m in cfg.models}
        assert by_name["Haiku 5.5"].efforts.levels[0] == "off"
        assert by_name["Sonnet 5.5"].efforts.levels[0] == "off"
        assert "off" not in by_name["Opus 5.5"].efforts.levels
        assert by_name["Opus 5.5"].efforts.levels == (
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        )
        assert all(m.efforts.default == "low" for m in by_name.values())


class TestParsing:
    def tag_and_show(self, name: str):
        tag = next(t for t in OLLAMA["tags"] if t["name"] == name)
        return tag, OLLAMA["show"][name]

    def test_embedding_models_are_not_chat_models(self) -> None:
        for name in ("bge-m3:latest", "qwen3-embedding:0.6b"):
            assert ollama_model(*self.tag_and_show(name), OllamaConfig()) is None

    def test_context_is_what_we_will_use_not_what_the_model_could(self) -> None:
        big = ollama_model(*self.tag_and_show("qwen3:8b"), OllamaConfig(num_ctx=8192))
        assert (big.ctx, big.ctx_max) == (8192, 40960)
        small = ollama_model(
            *self.tag_and_show("qwen2.5:3b"), OllamaConfig(num_ctx=65536)
        )
        assert (small.ctx, small.ctx_max) == (32768, 32768)

    def test_ollama_model_fields(self) -> None:
        model = ollama_model(*self.tag_and_show("gemma3:4b"), OllamaConfig())
        assert (model.id, model.tier, model.vision) == (
            "ollama/gemma3:4b",
            "local",
            True,
        )
        assert model.size_bytes and model.price_in is None

    def test_free_model(self) -> None:
        model = openrouter_model(record("google/gemma-4-31b-it:free"), TODAY)
        assert (model.id, model.tier, model.price_in) == (
            "openrouter/google/gemma-4-31b-it:free",
            "free",
            None,
        )
        assert not model.name.endswith("(free)")
        assert model.vision

    def test_paid_model_prices_are_per_million_tokens(self) -> None:
        model = openrouter_model(record("deepseek/deepseek-v4.1-flash-1017"), TODAY)
        assert (model.tier, model.price_in, model.price_out) == (
            "paid",
            Decimal("0.15"),
            Decimal("0.6"),
        )

    def test_safety_classifiers_are_not_offered(self) -> None:
        assert (
            openrouter_model(record("nvidia/nemotron-3.5-content-safety:free"), TODAY)
            is None
        )

    def test_expired_models_vanish_and_expiring_ones_say_when(self) -> None:
        raw = record("poolside/laguna-xs-2.1:free")
        assert openrouter_model(raw, date(2026, 10, 31)).expires == "2026-10-31"
        assert openrouter_model(raw, date(2026, 11, 1)) is None

    def test_json_shape(self) -> None:
        data = openrouter_model(
            record("deepseek/deepseek-v4.1-flash-1017"), TODAY
        ).to_json()
        assert data["price"] == {"in": "0.15", "out": "0.6"}
        assert data["efforts"] == {
            "levels": ("off", "low", "high", "max"),
            "default": "low",
        }
        json.dumps(data)


def make_catalog(
    tmp_path: Path, handler, environ=KEYS, today: date = TODAY, **config
) -> Catalog:
    cfg = Config(state_dir=tmp_path, **config)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    return Catalog(cfg, client, environ=environ, today=lambda: today)


@pytest.mark.anyio
class TestCatalog:
    async def test_lists_free_then_local_then_paid(self, tmp_path: Path) -> None:
        catalog = make_catalog(tmp_path, handler_for())
        await catalog.refresh()
        models = catalog.models()
        tiers = [m.tier for m in models]
        assert tiers == sorted(tiers, key=["free", "local", "paid"].index)
        ids = {m.id for m in models}
        assert "openrouter/google/gemma-4-31b-it:free" in ids
        assert "ollama/qwen3:8b" in ids
        assert {
            "anthropic/claude-haiku-5-5",
            "openrouter/deepseek/deepseek-v4.1-flash-1017",
        } <= ids
        assert not any("embed" in i or "bge" in i or "content-safety" in i for i in ids)

    async def test_show_runs_once_per_model_while_digests_hold(
        self, tmp_path: Path
    ) -> None:
        calls: list[str] = []
        catalog = make_catalog(tmp_path, handler_for(calls=calls))
        await catalog.refresh()
        first = calls.count("/api/show")
        assert first == len(OLLAMA["tags"])
        await catalog.refresh(force=True)
        assert calls.count("/api/show") == first

    async def test_missing_keys_disable_cloud_models_and_say_how_to_fix_it(
        self, tmp_path: Path
    ) -> None:
        catalog = make_catalog(tmp_path, handler_for(), environ={})
        await catalog.refresh()
        cloud = [
            m for m in catalog.models() if m.provider in ("openrouter", "anthropic")
        ]
        assert cloud and not any(m.available for m in cloud)
        assert any("AIHUB_ANTHROPIC_KEY" in (m.reason or "") for m in cloud)
        assert all(m.available for m in catalog.models() if m.provider == "ollama")

    async def test_default_is_the_first_usable_configured_model(
        self, tmp_path: Path
    ) -> None:
        with_keys = make_catalog(tmp_path, handler_for())
        await with_keys.refresh()
        assert with_keys.default_model() == "openrouter/google/gemma-4-31b-it:free"
        no_keys = make_catalog(tmp_path, handler_for(), environ={})
        await no_keys.refresh()
        assert no_keys.default_model() == "ollama/qwen3:8b"

    async def test_default_falls_back_to_any_local_model(self, tmp_path: Path) -> None:
        catalog = make_catalog(
            tmp_path,
            handler_for(),
            environ={},
            default_models=("ollama/not-installed:1b",),
        )
        await catalog.refresh()
        default = catalog.default_model()
        assert default is not None and default.startswith("ollama/")

    async def test_default_is_never_a_paid_model(self, tmp_path: Path) -> None:
        only_claude = make_catalog(
            tmp_path,
            handler_for(ollama_up=False, openrouter_up=False),
            environ={"AIHUB_ANTHROPIC_KEY": "sk-ant-test"},
        )
        await only_claude.refresh()
        assert any(m.available and m.tier == "paid" for m in only_claude.models())
        assert only_claude.default_model() is None

    async def test_local_models_stay_listed_but_disabled_while_ollama_is_down(
        self, tmp_path: Path
    ) -> None:
        up = {"on": True}

        def handle(request: httpx2.Request) -> httpx2.Response:
            return handler_for(ollama_up=up["on"])(request)

        catalog = make_catalog(tmp_path, handle)
        await catalog.refresh()
        up["on"] = False
        await catalog.refresh(force=True)
        local = [m for m in catalog.models() if m.provider == "ollama"]
        assert local and not any(m.available for m in local)
        assert local[0].reason == "Ollama isn't responding"
        assert catalog.status["ollama"]["ok"] is False

    async def test_openrouter_list_survives_a_restart_offline(
        self, tmp_path: Path
    ) -> None:
        online = make_catalog(tmp_path, handler_for())
        await online.refresh()
        cache = tmp_path / "openrouter-models.json"
        assert stat.S_IMODE(cache.stat().st_mode) == 0o600

        offline = make_catalog(tmp_path, handler_for(openrouter_up=False))
        await offline.refresh()
        assert offline.status["openrouter"]["ok"] is False
        assert "openrouter/google/gemma-4-31b-it:free" in {
            m.id for m in offline.models()
        }

    async def test_paid_models_are_still_listed_with_no_catalog_at_all(
        self, tmp_path: Path
    ) -> None:
        catalog = make_catalog(tmp_path, handler_for(openrouter_up=False))
        await catalog.refresh()
        ids = {m.id for m in catalog.models()}
        assert "openrouter/deepseek/deepseek-v4.1-flash-1017" in ids
        assert not any(i.endswith(":free") for i in ids)

    async def test_expired_free_models_drop_out_on_the_day(
        self, tmp_path: Path
    ) -> None:
        before = make_catalog(tmp_path, handler_for(), today=date(2026, 10, 31))
        await before.refresh()
        assert "openrouter/poolside/laguna-xs-2.1:free" in {
            m.id for m in before.models()
        }
        after = make_catalog(tmp_path, handler_for(), today=date(2026, 11, 1))
        await after.refresh()
        assert "openrouter/poolside/laguna-xs-2.1:free" not in {
            m.id for m in after.models()
        }

    async def test_get_returns_none_for_unknown_ids(self, tmp_path: Path) -> None:
        catalog = make_catalog(tmp_path, handler_for())
        await catalog.refresh()
        assert catalog.get("ollama/qwen3:8b").name == "qwen3:8b"
        assert catalog.get("ollama/nope") is None
