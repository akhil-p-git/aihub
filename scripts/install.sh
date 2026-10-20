#!/usr/bin/env bash
# Reproduce the aihub installs: the web chat (locked venv), Open WebUI (uv tool) and llmcord (pinned clone + locked venv).
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
LLMCORD_COMMIT=a99de6da1639f36972e6f57a4331e8d8b63b74bb
OPEN_WEBUI_VERSION=0.12.3

uv tool install --python 3.12 "open-webui==${OPEN_WEBUI_VERSION}"

[ -d "$ROOT/vendor/llmcord" ] || git clone https://github.com/jakobdylanc/llmcord.git "$ROOT/vendor/llmcord"
git -C "$ROOT/vendor/llmcord" checkout --quiet "$LLMCORD_COMMIT"
uv venv --python 3.12 "$ROOT/vendor/.venv-llmcord"
uv pip install --python "$ROOT/vendor/.venv-llmcord/bin/python" -r "$ROOT/config/llmcord-requirements.lock.txt"

# Web chat: the locked environment, a settings file to edit, and the user unit. Enable it with
# `systemctl --user enable --now aihub-web`.
(cd "$ROOT" && uv sync --frozen --python 3.12)
"$ROOT/.venv/bin/python" -m compileall -q "$ROOT/app"
install -d -m 700 "$ROOT/state/web"
mkdir -p "$HOME/.config/aihub" "$HOME/.config/systemd/user"
[ -e "$HOME/.config/aihub/web.yaml" ] || cp "$ROOT/config/web.example.yaml" "$HOME/.config/aihub/web.yaml"
ln -sfn "$ROOT/systemd/aihub-web.service" "$HOME/.config/systemd/user/aihub-web.service"
systemctl --user daemon-reload
