import contextlib
import os
import shutil
import socket
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

ZONEINFO = Path("/usr/share/zoneinfo")


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    """A private (mode 700) directory for an engine socket.

    Not under tmp_path: a Unix socket path is limited to about 100 bytes, which pytest's long test
    directory names overflow.
    """
    path = Path(tempfile.mkdtemp(prefix="aihub-img-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def bound_socket(socket_dir: Path) -> Iterator[str]:
    """Path of a socket file in a private directory that nothing listens on: all the catalog looks at."""
    path = socket_dir / "engine.sock"
    with contextlib.closing(socket.socket(socket.AF_UNIX)) as sock:
        sock.bind(str(path))
        os.chmod(path, 0o600)
        yield str(path)


@pytest.fixture
def tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Sets the process time zone for one test (the schedule math reads the system's, through the C library)."""

    def use(name: str) -> None:
        if not (ZONEINFO / name).exists():
            pytest.skip(f"no zone database for {name}")
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def cloud_model(monkeypatch: pytest.MonkeyPatch) -> str:
    """An Ollama cloud model in the model list, as Ollama reports one: its name ends in -cloud and the tag carries
    remote_host and remote_model. It is only a way for the prompt to reach ollama.com. Returns the model's id."""
    import helpers

    name = "gpt-oss:120b-cloud"
    tag = {
        "name": name,
        "model": name,
        "remote_model": "gpt-oss:120b",
        "remote_host": "https://ollama.com:443",
        "size": 384,
        "digest": "0" * 64,
    }
    monkeypatch.setitem(helpers.OLLAMA, "tags", [*helpers.OLLAMA["tags"], tag])
    monkeypatch.setitem(
        helpers.OLLAMA["show"], name, helpers.OLLAMA["show"]["qwen3:8b"]
    )
    return f"ollama/{name}"
