"""Run the web chat: `python -m aihub_web`."""

import logging
import sys

import uvicorn

from .config import ConfigError, load_config
from .server import create_app


def main() -> None:
    # journald stamps every line, so the format carries no time.
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        cfg = load_config()
    except ConfigError as err:
        sys.exit(f"aihub-web: {err}")
    uvicorn.run(
        create_app(cfg),
        host=cfg.host,
        port=cfg.port,
        log_config=None,
        server_header=False,
        timeout_graceful_shutdown=5,
    )


if __name__ == "__main__":
    main()
