# aihub

A private AI hub for one person: a web chat that puts local, free and paid models behind one composer, a Discord
relay bot, local Ollama models, free and cheap OpenRouter models, and a capped Claude API workspace. No secrets
live in this repo; they are in `~/.config/aihub/`.

## Layout
- `app/aihub_web/` the web chat: a small Starlette backend, and `static/` the browser UI (plain ES modules, no build step)
- `DESIGN.md` the design tokens and principles (`npx @google/design.md@0.4.0 lint DESIGN.md`)
- `config/` example settings and env files (`*.example`)
- `systemd/` user units, symlinked into `~/.config/systemd/user/`
- `scripts/` install, vendoring, benchmark and preflight scripts
- `tests/` Python tests; `tests/js/` browser-module unit tests; `tests/e2e/` browser end-to-end tests
- `state/` runtime data (gitignored): the chat database, Open WebUI's data
- `docs/` model notes

## Web chat (`aihub-web`)
Open <http://127.0.0.1:8787>. Enable it once with `systemctl --user enable --now aihub-web`; logs are
`journalctl --user -u aihub-web -f`; health is `curl -sf http://127.0.0.1:8787/api/health`.

**What it does.** Chats in a list grouped by day, with search; a model picker (free in the cloud, on this PC, paid)
and an intensity control in the composer; replies stream, with a Stop button; reasoning shows in a collapsed
"Thought for 8 s" block; each reply is footed with its model, intensity, speed, tokens, time and, for paid models,
its estimated cost. Light and dark themes follow the system and can be fixed in Settings. It works from a 360 px
phone up to an ultra-wide monitor.

**Models.** The list is built live: every model Ollama has installed that can chat, every `:free` model on
OpenRouter (refreshed every six hours, cached in `state/web/`), the paid OpenRouter models named in the settings
file, and Claude. A model whose provider has no key is listed but disabled, with the fix written next to it.
**A new chat always starts on the first usable model in `default_models`, which must be free or local**, so a
forgotten chat cannot spend the API credits; the settings file refuses a paid default.

**Keys** go in `~/.config/aihub/aihub.env` (shared with the Discord bot): `AIHUB_OPENROUTER_KEY` and
`AIHUB_ANTHROPIC_KEY`. Restart the service after editing. For Claude use a key created inside the capped Console
workspace; that spend limit, not this app, is the hard cap.

**Intensity** is the model's reasoning effort. The levels offered come from the model itself, so the control shows
only what that model accepts. New chats start at Low, or at Off where a model has no Low, so a reply is quick and cheap until you ask for more.

| Provider | Where the levels come from | What is sent |
|---|---|---|
| Ollama | `thinking.values` from `/api/show`: both `false` and `true` give Off and On; a model that lists only one value (qwen3:4b) or none (deepseek-r1:8b) always thinks, so there is no control | `think: false`, `true` or the level name ([docs](https://docs.ollama.com/capabilities/thinking)) |
| OpenRouter | the model's `reasoning.supported_efforts`; `none` is Off; a model with mandatory reasoning has no Off | `reasoning: {effort}` ([docs](https://openrouter.ai/docs/use-cases/reasoning-tokens)) |
| Anthropic | Low, Medium, High, Extra high, Max for every model; Off only where the model accepts it (Haiku 5.5 `disabled`, Sonnet 5.5 `between_tools`, set in the settings file); Opus 5.5 and Fable 5.1 cannot switch thinking off | `output_config.effort` plus `thinking: {type: adaptive, display: summarized}`, because the thinking text is hidden unless asked for ([docs](https://platform.claude.com/docs/en/build-with-claude/effort)) |

**Storage and retention.** Chats live only in `state/web/chats.db` (SQLite, mode 600, `secure_delete` on). A chat is
deleted 30 days after its last message (`retention_days`): once at start-up and then every hour, and the
checkpoint that follows moves the deleted text out of the write-ahead log too. Renaming a chat does not count as
activity. The sidebar says so, and a chat in its last week shows "Deletes in N days". Settings has "Delete all chats".

**Settings** are in `~/.config/aihub/web.yaml` (a copy of `config/web.example.yaml`; every value shown is the
default, and unknown keys are an error). It holds the port, `allowed_hosts`, retention, default models, the Ollama
context size, the paid OpenRouter models and their privacy routing, and the Claude price list used for the cost
shown under each reply.

**Security.** Binding is loopback only (the settings file refuses anything else). There is no login, because the
only way in is the loopback port: anyone who can reach it can read the chats and spend the credits. A page open in
your browser cannot use it: requests need an allowed `Host` (stops DNS rebinding) and an allowed `Origin` and JSON
content type on writes (stops cross-site posts). The page runs under a strict CSP with no inline script, style or
remote image, and model output is rendered through DOMPurify with an allowlist, so a prompt-injected reply cannot
run code or send the chat to a URL. The unit is sandboxed (`systemd-analyze --user security`: 1.6, OK).
To reach it from a phone or Mac, put Tailscale in front (not installed yet): follow `tailscale serve --help` for the
current syntax, point it at `http://127.0.0.1:8787`, and add the machine's tailnet name to `allowed_hosts`.

**Not built:** image or file attachments, editing a sent message, branching, tools or MCP, and Anthropic prompt
caching. Leaving a chat while it streams stops that reply (the part so far is kept). It has been checked in
Chromium; Firefox, Safari and iOS have not.

### Development
- Tests: `uv run pytest -q` (Python), `TZ=UTC node --test "tests/js/*.test.mjs"` (browser modules).
- End to end: `tests/e2e` drives the real app in a real browser against a stand-in that speaks the three providers'
  wire formats (`tests/e2e/fake_providers.py`). It needs Playwright and axe-core, which are deliberately not project
  dependencies: `cd tests/e2e && npm i --no-save --no-package-lock playwright axe-core && npx playwright install chromium`,
  then `node --test`. Set `E2E_LOG=1` to see the app's log.
- The axe scan injects a local copy of axe-core, because the page's CSP would block a CDN script.
- Browser libraries and fonts are vendored in `static/lib/` from pinned npm releases (integrity checked, listed in
  `static/lib/VENDOR.md`): `python3 scripts/vendor_frontend.py`.
- Python lock: `uv.lock` with `exclude-newer` two weeks back; JavaScript is formatted with Prettier (`.prettierrc.json`).
- A throwaway instance on another port with its own data: set `AIHUB_WEB_CONFIG` (a file with `port: 8788`) and
  `AIHUB_STATE_DIR`, then `PYTHONPATH=app .venv/bin/python -m aihub_web`.

## Open WebUI (installed, still running, localhost only)
Superseded by the web chat above, and kept until you decide to remove it
(`systemctl --user disable --now open-webui`, `uv tool uninstall open-webui`, then delete `state/open-webui`).
- Version pinned: `uv tool install --python 3.12 open-webui==0.12.3` (needs Python 3.11 or 3.12).
- Env file: `~/.config/aihub/open-webui.env` (chmod 600; template in `config/open-webui.env.example`).
- Unit: `systemctl --user enable --now open-webui`; logs: `journalctl --user -u open-webui -f`.
- Listens on `127.0.0.1:8080` only (`open-webui serve` defaults to `0.0.0.0`, so the unit passes `--host`).
- Health: `curl -sf http://127.0.0.1:8080/health` returns `{"status":true}`.

## Discord bot (llmcord, installed, not started)
- Pinned: llmcord `a99de6d` cloned into `vendor/` with a locked venv (`config/llmcord-requirements.lock.txt`,
  `pip-audit` clean). `scripts/install.sh` reproduces the installs.
- Config: copy `config/llmcord.example.yaml` to `~/.config/aihub/llmcord/config.yaml` and set your Discord user ID in
  `admin_ids` and `allowed_ids`. llmcord treats an empty `allowed_ids` as "everyone", so the unit runs
  `scripts/check_llmcord_config.py` first and refuses to start with an empty allowlist, a Claude default model
  (the first entry under `models` is the default after every restart) or a missing key.
- Secrets: `~/.config/aihub/aihub.env` (chmod 600, template `config/aihub.env.example`). The three names are
  `AIHUB_DISCORD_TOKEN`, `AIHUB_OPENROUTER_KEY`, `AIHUB_ANTHROPIC_KEY`.
- Start: `systemctl --user enable --now llmcord`; logs: `journalctl --user -u llmcord -f`.
- Claude through Anthropic's OpenAI-compatible endpoint has no prompt caching and bills Claude 5 thinking that it
  does not return. Use a key created inside the capped Console workspace and check the Console usage page.

## Not done yet
Keys for OpenRouter, Anthropic and Discord, Tailscale access from phone and Mac, and the model benchmark in
`docs/models.md` (`scripts/bench.py`, which refuses to run while another program is using the GPU).
