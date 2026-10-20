# aihub

Self-hosted AI hub: Open WebUI as the frontend, a Discord relay bot, local Ollama models, free and cheap
OpenRouter models, and a capped Claude API workspace. Config, systemd user units and notes only; no app code.
No secrets live here: they are in `~/.secrets.env` and `~/.config/aihub/`.

## Layout
- `systemd/` user units, symlinked into `~/.config/systemd/user/`
- `config/` example env and bot configs (`*.example`)
- `state/` runtime data (gitignored), e.g. Open WebUI's database
- `docs/` model notes and cost table

## Open WebUI (installed, localhost only)
- Version pinned: `uv tool install --python 3.12 open-webui==0.12.3` (needs Python 3.11 or 3.12).
- Env file: `~/.config/aihub/open-webui.env` (chmod 600; template in `config/open-webui.env.example`).
- Unit: `systemctl --user enable --now open-webui`; logs: `journalctl --user -u open-webui -f`.
- Listens on `127.0.0.1:8080` only (`open-webui serve` defaults to `0.0.0.0`, so the unit passes `--host`).
- Health: `curl -sf http://127.0.0.1:8080/health` returns `{"status":true}`.
- First visitor creates the admin account. Then set `ENABLE_SIGNUP=false` in the env file and restart the unit.

## Discord bot (llmcord, installed, not started)
- Pinned: llmcord `a99de6d` cloned into `vendor/` with a locked venv (`config/llmcord-requirements.lock.txt`,
  `pip-audit` clean). `scripts/install.sh` reproduces both installs.
- Config: copy `config/llmcord.example.yaml` to `~/.config/aihub/llmcord/config.yaml` and set your Discord user ID in
  `admin_ids` and `allowed_ids`. llmcord treats an empty `allowed_ids` as "everyone", so the unit runs
  `scripts/check_llmcord_config.py` first and refuses to start with an empty allowlist, a Claude default model
  (the first entry under `models` is the default after every restart) or a missing key.
- Secrets: `~/.config/aihub/aihub.env` (chmod 600, template `config/aihub.env.example`), read only by this unit.
  The three names are `AIHUB_DISCORD_TOKEN`, `AIHUB_OPENROUTER_KEY`, `AIHUB_ANTHROPIC_KEY`.
- Start: `systemctl --user enable --now llmcord`; logs: `journalctl --user -u llmcord -f`.
- Claude through Anthropic's OpenAI-compatible endpoint has no prompt caching and bills Claude 5 thinking that it
  does not return. Use a key created inside the capped Console workspace and check the Console usage page.
- Tests: `uv run --no-project --with pytest --with pyyaml pytest -q`.

## Not done yet
Connections for OpenRouter and Anthropic in Open WebUI (need keys), Tailscale access from phone and Mac, and the
model benchmark in `docs/models.md`.
