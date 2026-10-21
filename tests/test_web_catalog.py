import json
import os
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
    trusted_socket,
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

    def test_null_means_every_level_and_absent_means_none(self) -> None:
        """OpenRouter documents `supported_efforts: null` as "all effort values are accepted" and an omitted key as "no effort selection"."""
        assert openrouter_efforts(
            {"mandatory": False, "supported_efforts": None}
        ).levels == (
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        )
        assert (
            openrouter_efforts({"mandatory": True, "supported_efforts": None}).levels[0]
            == "minimal"
        )
        assert openrouter_efforts({"mandatory": False}) is None
        assert openrouter_efforts({"mandatory": False, "supported_efforts": []}) is None

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
        assert {(m.provider, m.reason) for m in cloud} == {
            (
                "anthropic",
                "Set AIHUB_ANTHROPIC_KEY in ~/.config/aihub/aihub.env, then restart aihub",
            ),
            (
                "openrouter",
                "Set AIHUB_OPENROUTER_KEY in ~/.config/aihub/aihub.env, then restart aihub",
            ),
        }
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


IMAGES_UNAVAILABLE = "Image engine isn't set up. See README, Images."


class TestTrustedSocket:
    def test_a_socket_in_a_private_directory_we_own(self, bound_socket: str) -> None:
        assert trusted_socket(bound_socket)

    def test_a_missing_socket_is_not_trusted(self, socket_dir: Path) -> None:
        assert not trusted_socket(str(socket_dir / "engine.sock"))
        assert not trusted_socket(str(socket_dir / "absent" / "engine.sock"))

    @pytest.mark.parametrize("mode", [0o750, 0o705, 0o755, 0o777])
    def test_a_directory_others_can_reach_is_not_trusted(
        self, bound_socket: str, mode: int
    ) -> None:
        directory = Path(bound_socket).parent
        directory.chmod(mode)
        try:
            assert not trusted_socket(bound_socket)
        finally:
            directory.chmod(0o700)

    def test_a_regular_file_is_not_a_socket(self, socket_dir: Path) -> None:
        path = socket_dir / "engine.sock"
        path.write_text("not a socket")
        assert not trusted_socket(str(path))

    def test_a_symlink_to_a_socket_is_not_trusted(
        self, bound_socket: str, socket_dir: Path
    ) -> None:
        link = socket_dir / "link.sock"
        link.symlink_to(bound_socket)
        assert not trusted_socket(str(link))

    def test_a_symlinked_directory_is_not_trusted(
        self, bound_socket: str, socket_dir: Path
    ) -> None:
        link = socket_dir / "dir-link"
        link.symlink_to(socket_dir)
        assert not trusted_socket(str(link / "engine.sock"))

    @staticmethod
    def owned_by_someone_else(monkeypatch: pytest.MonkeyPatch, path: str) -> None:
        """Make lstat report `path` (and only it) as owned by another user."""
        real = os.lstat

        def lstat(target, *args, **kwargs):
            result = real(target, *args, **kwargs)
            if str(target) != path:
                return result
            fields = list(result)
            fields[4] += 1  # st_uid
            return os.stat_result(fields)

        monkeypatch.setattr(os, "lstat", lstat)

    def test_a_socket_someone_else_owns_is_not_trusted(
        self, bound_socket: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.owned_by_someone_else(monkeypatch, bound_socket)
        assert not trusted_socket(bound_socket)

    def test_a_directory_someone_else_owns_is_not_trusted(
        self, bound_socket: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self.owned_by_someone_else(monkeypatch, str(Path(bound_socket).parent))
        assert not trusted_socket(bound_socket)


@pytest.mark.anyio
class TestImageModels:
    async def refreshed(self, tmp_path: Path, **config) -> Catalog:
        catalog = make_catalog(tmp_path, handler_for(), **config)
        await catalog.refresh()
        return catalog

    async def test_nothing_changes_while_no_socket_is_set(self, tmp_path: Path) -> None:
        catalog = await self.refreshed(tmp_path)
        assert not any(m.output == "image" for m in catalog.models())
        assert set(catalog.status) == {"ollama", "openrouter", "anthropic"}
        assert catalog.images_status() == "off"
        for model in catalog.models():
            assert not {"output", "edits", "est_s", "note"} & set(model.to_json())

    async def test_image_models_are_listed_after_the_local_ones_when_a_socket_is_set(
        self, tmp_path: Path, bound_socket: str
    ) -> None:
        catalog = await self.refreshed(tmp_path, images={"socket": bound_socket})
        models = catalog.models()
        images = [m for m in models if m.output == "image"]
        assert [m.id for m in images] == ["images/zimage", "images/klein4b"]
        assert all(m.provider == "images" and m.tier == "local" for m in images)
        assert all(m.available and m.reason is None for m in images)
        assert all(m.efforts is None and m.price_in is None for m in images)
        tiers = [m.tier for m in models]
        assert tiers == sorted(tiers, key=["free", "local", "paid"].index)
        assert models.index(images[0]) > max(
            i for i, m in enumerate(models) if m.provider == "ollama"
        )
        assert catalog.get("images/klein4b") == images[1]
        assert catalog.images_status() == "ok"
        assert catalog.status["images"] == "ok"

    async def test_image_models_say_what_they_are_in_json(
        self, tmp_path: Path, bound_socket: str
    ) -> None:
        catalog = await self.refreshed(tmp_path, images={"socket": bound_socket})
        zimage = catalog.get("images/zimage").to_json()
        klein = catalog.get("images/klein4b").to_json()
        assert (zimage["output"], zimage["edits"], zimage["note"]) == (
            "image",
            False,
            "Best with text in images.",
        )
        assert klein["edits"] is True
        assert zimage["est_s"] == {"square": 23, "wide": 12, "tall": 12}
        assert (zimage["name"], zimage["tier"], zimage["efforts"]) == (
            "Z-Image Turbo",
            "local",
            None,
        )
        json.dumps([zimage, klein])

    async def test_the_models_come_from_the_settings(
        self, tmp_path: Path, bound_socket: str
    ) -> None:
        only = {
            "id": "only",
            "name": "Only one",
            "steps": 3,
            "est_s": {"square": 2, "wide": 1, "tall": 1},
        }
        catalog = await self.refreshed(
            tmp_path, images={"socket": bound_socket, "models": [only]}
        )
        assert [m.id for m in catalog.models() if m.output == "image"] == [
            "images/only"
        ]

    async def test_a_missing_socket_lists_them_unavailable_with_the_fix(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        catalog = await self.refreshed(
            tmp_path, images={"socket": str(socket_dir / "engine.sock")}
        )
        images = [m for m in catalog.models() if m.output == "image"]
        assert len(images) == 2
        assert not any(m.available for m in images)
        assert {m.reason for m in images} == {IMAGES_UNAVAILABLE}
        assert catalog.status["images"] == "down"

    async def test_trust_is_checked_on_every_listing_not_cached(
        self, tmp_path: Path, bound_socket: str
    ) -> None:
        catalog = await self.refreshed(tmp_path, images={"socket": bound_socket})
        assert catalog.get("images/zimage").available
        directory = Path(bound_socket).parent
        directory.chmod(0o755)
        try:
            assert not catalog.get("images/zimage").available
            assert catalog.status["images"] == "down"
        finally:
            directory.chmod(0o700)
        assert catalog.get("images/zimage").available

    async def test_an_image_model_is_never_the_default(
        self, tmp_path: Path, bound_socket: str
    ) -> None:
        """Even when it is the only usable model, and even though it counts as local."""
        catalog = make_catalog(
            tmp_path,
            handler_for(ollama_up=False, openrouter_up=False),
            environ={},
            images={"socket": bound_socket},
        )
        await catalog.refresh()
        assert any(m.available and m.output == "image" for m in catalog.models())
        assert catalog.default_model() is None

    async def test_the_default_is_unchanged_with_image_models_present(
        self, tmp_path: Path, bound_socket: str
    ) -> None:
        with_images = await self.refreshed(tmp_path, images={"socket": bound_socket})
        without = await self.refreshed(tmp_path)
        assert with_images.default_model() == without.default_model()
        assert with_images.default_model() == "openrouter/google/gemma-4-31b-it:free"

    async def test_a_missing_openrouter_key_does_not_touch_image_models(
        self, tmp_path: Path, bound_socket: str
    ) -> None:
        catalog = await self.refreshed(
            tmp_path, environ={}, images={"socket": bound_socket}
        )
        assert catalog.get("images/zimage").available
