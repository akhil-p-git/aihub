import contextlib
import os
import shutil
import socket
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


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
