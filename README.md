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
- `desktop/` the Linux desktop app: an Electron shell around the web chat, its tests, and the scripts that build and package it
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
activity. The sidebar says so, and a chat in its last week shows "Deletes in N days". Settings has "Delete all chats" (projects and scheduled tasks stay).

**Upgrading the database.** The schema has a version (`PRAGMA user_version`: 2 added images, 3 added projects, 4 added scheduled tasks). Starting a
newer build on an older file upgrades it in place, one transaction per step, and first copies the file with SQLite's
backup API to `chats.db.v<old>.bak` beside it (mode 600; a second copy of the same version is `chats.db.v<old>-2.bak`).
The copy is built as `chats.db.v<old>.bak.part` and only gets its own name once it is whole, so a crash leaves a part
(which the next start removes), never a broken file under the name a restore uses. If the copy cannot be made, the file
is not upgraded. A copy holds chats as they were, so it is not kept for long: it is removed `retention_days` after it was
made (a chat that was due to expire soon after the upgrade can live on in the copy for up to that long) and at once by
"Delete all chats". Copy it somewhere else first if you want to keep it longer. A copy that cannot be removed (say you
made it read-only) is left, and the log says so. An older build does not know about copies, so after going back, delete
them by hand. An older build
refuses a newer file (`database schema 3 is newer than this app (2)`), and setting `user_version` back by hand does not
help, because from version 3 the `chats` table has a `project_id` column that older code cannot read. To go back, stop the
service, put the copy over the database (`cp state/web/chats.db.v2.bak state/web/chats.db`), delete `chats.db-wal` and
`chats.db-shm` beside it, and start the older build. Chats made after the upgrade are not in the copy. The desktop app
keeps its own database and its frozen copy of the backend, so after pulling a change that raises the schema version, run
`build:backend` and `dist:linux` again before pointing it at a newer database.

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

**Not built:** image or file attachments, editing a sent message, editing an image, branching, tools or MCP, and
Anthropic prompt caching. Leaving a chat while a text reply streams stops that reply (the part so far is kept);
an image being made carries on (see Images). It has been checked in Chromium; Firefox, Safari and iOS have not.

### Projects
A project is a name and some instructions that the chats in it share. The sidebar has a "Projects" section above the
day groups: "New project" asks for a name (up to 80 characters) and instructions (up to 4,000), and each project is a
row you open to see its chats, with "New chat in <project>" and an options menu (Edit, Delete). A chat made from the
project, or moved into it from its own menu ("Move to project…", or "No project" to take it out), is told the
project's instructions after your standing ones, at every message, so an edit applies to the next message of a chat
already in it. The title line says "Project: <name>". Image models are not given them: an image prompt goes to the
engine as typed.

Instructions use up a model's memory like anything else: about three characters make a token, the dialog shows the
count, and the whole system prompt is taken off what the chat history may use. A local model has 8,192 tokens here, so
keep project instructions short; 4,000 characters is a fifth of the room a long chat has.

Deleting a project keeps its chats (they show with the others) unless you tick "Also delete its chats", which removes
them with their text and images. A project is settings, not chat content, so the 30-day retention does not remove it
and "Delete all chats" leaves it. There can be 100 projects. Which projects are open is remembered per browser. The
list holds the newest 200 chats in all, so a project's oldest chats may not show in it once more than that are newer.
Project files are not built: a local model's window is too small to paste them in.

### Scheduled tasks
A scheduled task is a prompt that aihub sends for you on a schedule, to a model on this PC. Each run makes a new chat
named "<task>, <date>" (in the task's project, if it has one) with the prompt and the reply in it. That chat is an
ordinary one, with a clock before its title in the list and the usual 30 days of retention. "Scheduled tasks" in the
sidebar, above Settings, opens the page: one card for each task, with an on/off switch, the schedule in words, the
model, how the last run went (with a link to its chat), when the next one is, and the buttons Run now, Edit and Delete.

A schedule is every 1, 2, 3, 4, 6, 8 or 12 hours (on the hour), every day, every weekday, or one day a week, on this
PC's clock. When the clocks go forward, a time that was skipped runs the moment the clock passes it; when they go back,
a time that happens twice runs once.

Only local models, and the server enforces it, not the page. Making or editing a task with a cloud or image model is
refused, a run checks again, and a model that is gone makes the run fail instead of falling back to another. A run sends
the saved prompt with your standing instructions and the project's, the model has no tools, and it reads nothing else,
so nothing a task does costs money or goes to a cloud provider.

Tasks run inside `aihub-web` (or the desktop app), so only while it is running; there is no separate service. One task
runs at a time, whether the schedule or Run now started it, and a run that finds replies streaming or an image being
made waits for the next check (every 30 seconds). A run is stopped after 15 minutes. A task that was due while aihub was
off runs once when it starts if it was due less than 12 hours before, and is skipped (the card says "Skipped") if it
was due longer ago; several missed times count as one. Closing aihub in the middle of a run keeps what had arrived and
does not run it again. A run that fails, is stopped, or gets no answer counts as a failure: three in a row switch the
task off, and its card says why. Switching it on again starts the count afresh.

There can be 50 tasks. They are settings, so the 30-day retention does not remove them and "Delete all chats" leaves
them; deleting a task keeps its chats. The next run of each is stored, set from its schedule and never from its last
run, so a clock that is set back cannot run a task twice.

### Images
Optional, and free: the images are made on this PC's GPU by stable-diffusion.cpp's `sd-server`, which runs in its own
sandboxed user unit with no network (`systemd/aihub-images.service`, `docs/adr/0001-image-engine.md`). Nothing is sent
anywhere and nothing is billed. While `images.socket` in `web.yaml` is unset, the page shows nothing about it.

**Turn it on.** Put the engine in `~/work/aihub-images/` (`bin/sd-server` and, under `models/`, the files that
`systemd/images/models.json` names; there is no build or fetch script yet), link the two units
from the checkout (`systemctl --user link "$PWD/systemd/aihub-images.socket" "$PWD/systemd/aihub-images.service"`, which wants
absolute paths, then `systemctl --user enable --now aihub-images.socket`), set `images: {socket: /run/user/1000/aihub-images/engine.sock}` in
`~/.config/aihub/web.yaml`, and restart the service. `scripts/images/isolation_probe.sh` checks the unit's cage. The
socket starts the engine on the first image and the engine exits after five idle minutes. If the units stop with
`start-limit-hit`, `systemctl --user reset-failed aihub-images.socket aihub-images.service` and start the socket again.
A socket that is missing or not owned by you shows the image models as unavailable, with this section named as the fix.

**Using it.** The model picker lists a "Makes images" group next to "On this PC": Z-Image Turbo (the default, best at
text in images) and FLUX.2 klein 4B (faster drafts, but it often misspells text). Choosing one puts the composer in image
mode: the box is an "Image prompt", Send becomes "Create image", and a Shape chip (Square 1024 by 1024, Wide 1024 by 576,
Tall 576 by 1024) takes the intensity chip's place and names the usual time for each shape. A box of signal bars shows
the steps while the image is made. Stop ends the image (and the engine, which starts again for the next one); moving to
another chat does not stop it, and coming back shows the progress and then the image. Under a finished image: Download (a `.webp`), New seed and "Redo with" the other
model (both make the same prompt again as a new turn), Reuse prompt (the words go back in the box) and Full size.
Choosing a text model leaves image mode. Editing an image is not built.

**What it costs and what it blocks.** One image at a time uses the whole GPU, so while one is made local chat models
answer "paused" and cloud models keep working; a second image waits up to two minutes for its turn. Each image has a time
limit of three times its estimate plus a minute, at most five. The first image after the engine has been idle also loads the
model, which adds to its time.

**Storage.** Images live in `state/web/chats.db` as WebP (the schema is version 2, one `images` table) and go with their
chat, when it expires or is deleted. Image storage stops at 2 GiB, after which new images are refused until chats are
deleted. Code from before images refuses a database of version 2 or later: see "Upgrading the database" under Storage
and retention for how to go back.

**Exposure.** The engine is never reachable over the network, but the page's port is: if you put Tailscale in front of
aihub (see Security), everyone the `allowed_hosts` list admits can make images and keep the GPU busy.

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

## Desktop app (Linux x86_64, this PC)
The web chat in a window of its own: an Electron shell (`desktop/`) starts the same backend as a child process on a
random loopback port and shows it. The page, the models and the settings are the web chat's; nothing is reimplemented.

**Build** (no sudo; the first three steps fetch pinned downloads, `dist:linux` uses no network):

    cd desktop
    npm ci                                  # the pinned Electron package, without its binary
    node node_modules/electron/install.js   # the binary: fetched once into ~/.cache/electron and checked against checksums.json
    npm run build:backend                   # desktop/payload/linux-x64: CPython 3.12 and the locked dependencies, about 52 MiB
    npm run dist:linux                      # desktop/dist/linux-unpacked/: the app, about 334 MiB, built in under a second

Run `desktop/dist/linux-unpacked/aihub-desktop`. The packaged app holds a frozen copy of the backend, so after a change
under `app/` run `build:backend` and `dist:linux` again. `npm start` runs the shell from the checkout instead, for
development (it does the Electron install step itself): it uses the repo's `.venv` and keeps its data in
`~/.config/aihub-desktop-dev/`.

**Keys and data.** The provider keys are read from `~/.config/aihub/aihub.env`, the file the service uses; the app never
writes it. With no keys, or empty ones, it starts on the local Ollama model, as the service does. Its own data is in
`~/.config/aihub-desktop/`: `state/chats.db` (its chat history, kept apart from the service's `state/web/chats.db`),
an optional `web.yaml` (the same settings as the service's), `desktop.json` (the port of the last run, which it tries
first), `pycache/`, `logs/backend.log` (the previous run's log is kept as `backend.log.1`) and Chromium's profile. To carry
the service's history over, stop the service and copy `state/web/chats.db` to `~/.config/aihub-desktop/state/`.
Rebuild the app first if the service's database has image tables (schema version 2): an app built before the image work
refuses to open one.

**What differs from the browser page.**
- Every launch picks a new random token. The backend refuses `/api/` requests that lack the `aihub_t` cookie, which only the
  app's own window holds, so another program on this machine cannot use the app's port (it gets 401). The service sets no
  token and stays open to the local user, as before.
- One window. Closing it quits the app and stops the backend; if the app is killed, the backend stops within a second.
  A second launch focuses the first window.
- A link to another site opens in the default browser, and nothing else opens in the window. There are no developer tools.
- It renders in software. On this machine Chromium's GPU process crashes at times with the NVIDIA driver (the comment in
  `desktop/src/main.js` has the details), so the app holds no GPU memory and no `/dev/nvidia*` file.

**Resources**, measured at rest with the chat open: 9 processes, about 335 MiB of memory (PSS; 55 MiB of it the backend),
no GPU memory, and the chat shows 1.1 s after launch.

**The backend's side of the contract.** All of it is optional, and the service uses none of it:
`AIHUB_PORT` (0 picks a free port; it overrides `port` in `web.yaml`), `AIHUB_LAUNCH_TOKEN` (when set, `/api/` needs the
`aihub_t` cookie with that value), and `AIHUB_EXIT_ON_STDIN_EOF=1` (the backend stops, as `systemctl stop` would, when its
stdin ends, which is how it follows the app). Once it accepts connections it prints `{"event":"ready","port":N}` on stdout;
exit status 3 means it never started and 1 a bad setting.

**Installing it and a menu entry** are optional. To keep the app you run apart from the checkout, so that a rebuild or a
branch change cannot touch it, copy it out: `rsync -a --delete desktop/dist/linux-unpacked/ ~/.local/opt/aihub-desktop/`
(close the app first; the same command updates it after a rebuild). Then save this as
`~/.local/share/applications/dev.akhil-p.aihub.desktop`. The path in `Exec` must be absolute, and there is no custom
icon, so Electron's is used:

    [Desktop Entry]
    Type=Application
    Name=aihub
    Comment=Chat with local, free and paid models
    Exec=/home/akhil/.local/opt/aihub-desktop/aihub-desktop
    Terminal=false
    Categories=Network;Chat;
    StartupWMClass=dev.akhil-p.aihub

**If it does not start.** Started with a stripped environment (`env -i`, a cron job), the app needs `XAUTHORITY` as well as
the display variables: on KDE Wayland it runs through XWayland, which refuses a client without it, and Chromium then
crashes at start ("Authorization required, but no authorization protocol specified", one core dump each time). From the
menu or a terminal of the session it needs nothing extra.

**Uninstall.** Delete `desktop/dist/`, `desktop/payload/`, `desktop/.cache/` and `desktop/node_modules/` (git ignores
them), `~/.local/opt/aihub-desktop/` and the `.desktop` file if you installed them, `~/.config/aihub-desktop/` (this
deletes the app's chat history) and `~/.config/aihub-desktop-dev/`, and `~/.cache/electron/` (Electron's download cache, which other Electron projects share). The keys file
belongs to the service as well and stays.

**Rebuild checklist.** Electron `44.6.0` is pinned exactly in `desktop/package.json`; to move, `npm install --save-dev
--save-exact electron@X` in `desktop/` (its `.npmrc` sets `min-release-age=14`), then `npm test`. The Python is
python-build-standalone `20261003` (CPython 3.12.15): to move, change `PBS_TAG`, `PYTHON_VERSION` and `ARCHIVE_SHA256` in
`desktop/scripts/build-backend.mjs` (the sha256 is in the release's `SHA256SUMS`). The Python dependencies come from
`uv.lock`. `cd desktop && npm test` runs the shell's tests.

**Known limit.** A request that is already past the token check when the backend dies could reach a program that takes
the freed port in the few milliseconds before the app notices, and it would carry the dead backend's cookie. That cookie
is worthless to the next backend, because every start has a new token, but only a check of the peer's process would
close the window.

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

The project is under the MIT License (`LICENSE`). Vendored browser libraries and fonts keep their own licenses, which sit beside them in `app/aihub_web/static/lib/`.
