"""Run the web chat: `python -m aihub_web`.

A launcher that owns the process (the desktop shell) relies on three things:
- once the server accepts connections it prints one line on stdout, `{"event":"ready","port":N}`;
- with AIHUB_EXIT_ON_STDIN_EOF=1 it stops, as `systemctl stop` would, when its stdin reaches EOF;
- exit status 3 means the server never started (the port was taken or startup failed), 1 a bad setting.
"""

import contextlib
import json
import logging
import os
import signal
import socket
import sys
import threading

import uvicorn
from uvicorn.config import STARTUP_FAILURE

from .config import ConfigError, load_config
from .server import create_app

log = logging.getLogger("aihub_web")


class _Server(uvicorn.Server):
    """uvicorn's server, announcing itself on stdout once it is up."""

    async def startup(self, sockets: list[socket.socket] | None = None) -> None:
        # The lifespan startup (models loaded, database open) and the bind both happen in here.
        await super().startup(sockets=sockets)
        if self.started:
            # Port 0 is only resolved by the bind, so read the port from the socket. `servers` is not a
            # documented uvicorn API; uvicorn is pinned and test_web_launch.py fails if it moves.
            port = self.servers[0].sockets[0].getsockname()[1]
            print(
                json.dumps({"event": "ready", "port": port}, separators=(",", ":")),
                flush=True,
            )


def _stop_when_stdin_closes() -> None:
    """Send SIGTERM to this process at the end of stdin: the parent that owns the pipe is gone.

    SIGTERM is the signal `systemctl stop` sends, so a streaming reply is stored as stopped
    through the shutdown that already exists.
    """
    # Read the descriptor, not sys.stdin.buffer: a thread blocked inside BufferedReader.read holds that
    # object's lock, and the interpreter then aborts (SIGABRT, status -6) at exit because it can't take
    # the lock. A start that failed would end that way instead of with status 3.
    with contextlib.suppress(OSError):  # a stdin that can't be read counts as closed
        while os.read(0, 4096):
            pass
    log.info("stdin closed, stopping")
    os.kill(os.getpid(), signal.SIGTERM)


def main() -> None:
    # journald stamps every line, so the format carries no time.
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        cfg = load_config()
    except ConfigError as err:
        sys.exit(f"aihub-web: {err}")
    if os.environ.get("AIHUB_EXIT_ON_STDIN_EOF") == "1":
        threading.Thread(target=_stop_when_stdin_closes, daemon=True).start()
    server = _Server(
        uvicorn.Config(
            create_app(cfg),
            host=cfg.host,
            port=cfg.port,
            log_config=None,
            server_header=False,
            timeout_graceful_shutdown=5,
        )
    )
    # After Ctrl+C has stopped the server gracefully, uvicorn raises KeyboardInterrupt out of Server.run.
    # uvicorn.run swallows it, so Ctrl+C ends with status 0 and no traceback; calling Server.run leaves
    # that to us.
    with contextlib.suppress(KeyboardInterrupt):
        server.run()
    # uvicorn.run ends with this check, so Server.run leaves it to us. uvicorn 0.54.0 already calls
    # sys.exit(STARTUP_FAILURE) from Server.startup when the bind or the lifespan fails (test_web_launch.py
    # pins both); the check keeps status 3 should a run ever return without having started.
    if not server.started:
        sys.exit(STARTUP_FAILURE)


if __name__ == "__main__":
    main()
