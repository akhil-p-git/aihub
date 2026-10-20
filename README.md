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

## Not done yet
Connections for OpenRouter and Anthropic (need keys), Tailscale access from phone and Mac, the Discord bot
(llmcord), and the model benchmark in `docs/models.md`.
