from pathlib import Path

import pytest
import yaml
from check_llmcord_config import check

ENV = {
    "AIHUB_DISCORD_TOKEN": "t",
    "AIHUB_OPENROUTER_KEY": "k",
    "AIHUB_ANTHROPIC_KEY": "k",
}
EXAMPLE = Path(__file__).parents[1] / "config" / "llmcord.example.yaml"


def good() -> dict:
    return {
        "bot_token_env": "AIHUB_DISCORD_TOKEN",
        "permissions": {"users": {"admin_ids": [1], "allowed_ids": [1]}},
        "providers": {
            "openrouter": {"api_key_env": "AIHUB_OPENROUTER_KEY"},
            "anthropic": {"api_key_env": "AIHUB_ANTHROPIC_KEY"},
            "ollama": {},
        },
        "models": {
            "openrouter/deepseek/x": None,
            "anthropic/claude-haiku-5-5": None,
            "ollama/qwen3:8b": None,
        },
    }


def test_good_config_passes() -> None:
    assert check(good(), ENV) == []


@pytest.mark.parametrize("key", ["allowed_ids", "admin_ids"])
@pytest.mark.parametrize("bad", [[], None, [True], ["123"]])
def test_user_lists_must_be_real_ids(key: str, bad: object) -> None:
    config = good()
    config["permissions"]["users"][key] = bad
    assert any(key in problem for problem in check(config, ENV))


def test_missing_permissions_block_fails_closed() -> None:
    config = good()
    del config["permissions"]
    assert len(check(config, ENV)) == 2


def test_claude_default_model_rejected() -> None:
    config = good()
    config["models"] = {
        "anthropic/claude-haiku-5-5": None,
        "openrouter/deepseek/x": None,
    }
    assert any(
        "must not be a Claude model" in problem for problem in check(config, ENV)
    )


def test_unknown_provider_rejected() -> None:
    config = good()
    config["models"]["mystery/model"] = None
    assert any("unknown provider" in problem for problem in check(config, ENV))


@pytest.mark.parametrize(
    "var", ["AIHUB_DISCORD_TOKEN", "AIHUB_OPENROUTER_KEY", "AIHUB_ANTHROPIC_KEY"]
)
@pytest.mark.parametrize("value", [None, ""])
def test_missing_or_empty_secret_rejected(var: str, value: str | None) -> None:
    env = {k: v for k, v in ENV.items() if k != var}
    if value is not None:
        env[var] = value
    assert check(good(), env)


def test_shipped_example_is_not_open_by_default() -> None:
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    problems = check(config, ENV)
    assert len(problems) == 2 and all("Discord user IDs" in p for p in problems)


def test_shipped_example_passes_once_ids_are_set() -> None:
    config = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    config["permissions"]["users"].update(admin_ids=[1], allowed_ids=[1])
    assert check(config, ENV) == []
