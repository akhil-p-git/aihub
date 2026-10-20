#!/usr/bin/env python3
"""Fail-closed preflight for the llmcord unit: refuse to start with an open or unusable config.

llmcord treats an empty `allowed_ids` list as "everyone is allowed", so a missing ID would turn the
bot into an open relay that spends the API credits. Run as `check_llmcord_config.py <config.yaml>`;
exit status 1 (with reasons on stderr) stops the unit from starting.
"""

import os
import sys
from collections.abc import Mapping
from pathlib import Path

import yaml


def _is_id_list(value: object) -> bool:
    return (
        isinstance(value, list) and bool(value) and all(type(i) is int for i in value)
    )


def check(config: dict, environ: Mapping[str, str]) -> list[str]:
    problems: list[str] = []

    users = (config.get("permissions") or {}).get("users") or {}
    for key in ("allowed_ids", "admin_ids"):
        if not _is_id_list(users.get(key)):
            problems.append(
                f"permissions.users.{key} must be a non-empty list of Discord user IDs (empty means everyone)"
            )

    models = config.get("models") or {}
    providers = config.get("providers") or {}
    if not models:
        problems.append("models is empty")
    else:
        default = next(iter(models))
        if default.startswith("anthropic/"):
            problems.append(
                f"the first model is the default after every restart and must not be a Claude model: {default}"
            )

    for name in sorted({model.split("/", 1)[0] for model in models}):
        provider = providers.get(name)
        if provider is None:
            problems.append(f"a model uses unknown provider {name!r}")
        elif "api_key_env" in provider and not environ.get(provider["api_key_env"]):
            problems.append(
                f"provider {name!r}: env var {provider['api_key_env']} is unset or empty"
            )

    token_env = config.get("bot_token_env")
    if not token_env or not environ.get(token_env):
        problems.append("bot_token_env is missing or its env var is unset or empty")

    return problems


def main() -> int:
    config = yaml.safe_load(Path(sys.argv[1]).read_text(encoding="utf-8")) or {}
    problems = check(config, os.environ)
    for problem in problems:
        print(f"llmcord preflight: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
