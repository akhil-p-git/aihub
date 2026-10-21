"""Starting `python -m aihub_web` the way a launcher does: a ready line, a stop on stdin EOF, and exit status 3."""

import contextlib
import functools
import json
import os
import re
import secrets
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
from aihub_web.db import Store
from test_web_shutdown import free_port, wait_until_up

ROOT = Path(__file__).parents[1]
# Seconds the desktop shell waits after SIGTERM before it kills the backend. A stop mid-reply takes the
# 5 s grace period of the server plus its own shutdown.
STOP_BUDGET = 7
READY = re.compile(r'\{"event":"ready","port":[0-9]+\}\n')
RETURNS_WITHOUT_STARTING = """
import uvicorn

async def startup(self, sockets=None):
    self.should_exit = True  # give up without binding and without exiting

uvicorn.Server.startup = startup
from aihub_web.__main__ import main

main()
"""


@dataclass
class Launch:
    process: subprocess.Popen
    log_path: Path

    def log(self) -> str:
        return self.log_path.read_text()

    def ready_line(self, timeout: float = 20) -> str:
        assert self.process.stdout is not None
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        line = self.process.stdout.readline() if ready else ""
        assert line, (
            f"no ready line (exit status {self.process.poll()}); log:\n{self.log()}"
        )
        return line

    def port(self) -> int:
        return json.loads(self.ready_line())["port"]

    def stdout_rest(self) -> str:
        assert self.process.stdout is not None
        return self.process.stdout.read()

    def stop(self) -> None:
        """What `systemctl stop` does: SIGTERM, then a clean exit."""
        self.process.send_signal(signal.SIGTERM)
        assert self.process.wait(timeout=20) in (0, -signal.SIGTERM), self.log()


@pytest.fixture(scope="module")
def fake_url():
    """Stand-ins for Ollama, OpenRouter and Anthropic, so no launch test reaches a real provider."""
    port = free_port()
    fake = subprocess.Popen(
        [sys.executable, str(ROOT / "tests" / "e2e" / "fake_providers.py"), str(port)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        wait_until_up(f"http://127.0.0.1:{port}/api/tags", fake)
        yield f"http://127.0.0.1:{port}"
    finally:
        fake.kill()
        fake.wait()


@pytest.fixture
def occupied_port():
    """A port that something is listening on, so binding it fails."""
    with socket.socket() as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen()
        yield blocker.getsockname()[1]


@pytest.fixture
def launch(tmp_path: Path, fake_url: str):
    """Start the app on a config of its own; whatever is still running when the test ends is killed."""
    started: list[Launch] = []

    def start(
        *,
        stdin=subprocess.DEVNULL,
        config_port: int | None = None,
        args: tuple[str, ...] = ("-m", "aihub_web"),
        **env: str,
    ) -> Launch:
        config = tmp_path / "web.yaml"
        config.write_text(
            f"port: {free_port() if config_port is None else config_port}\n"
            f'ollama: {{ url: "{fake_url}" }}\n'
            f'openrouter: {{ url: "{fake_url}/api/v1" }}\n'
            f'anthropic: {{ url: "{fake_url}" }}\n'
        )
        log_path = tmp_path / "app.log"
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [sys.executable, *args],
                env={
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": str(ROOT / "app"),
                    "AIHUB_WEB_CONFIG": str(config),
                    "AIHUB_STATE_DIR": str(tmp_path / "state"),
                    "AIHUB_OPENROUTER_KEY": "test-key",
                    **env,
                },
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=log,
                text=True,
            )
        started.append(Launch(process, log_path))
        return started[-1]

    yield start
    for app in started:
        if app.process.poll() is None:
            app.process.kill()
        app.process.wait()
        for pipe in (app.process.stdin, app.process.stdout):
            if pipe:
                pipe.close()


@pytest.fixture(params=["service", "desktop"])
def start(request, launch):
    """`launch` the way systemd starts the app, then the way the desktop shell does.

    systemd gives the unit stdin on /dev/null and sets no new variable. The shell holds a pipe open on
    the child's stdin for as long as it lives and sets AIHUB_EXIT_ON_STDIN_EOF=1.
    """
    if request.param == "service":
        return launch
    return functools.partial(launch, stdin=subprocess.PIPE, AIHUB_EXIT_ON_STDIN_EOF="1")


def test_port_zero_prints_one_ready_line_naming_a_port_that_serves(
    launch, occupied_port: int
) -> None:
    app = launch(config_port=occupied_port, AIHUB_PORT="0")
    line = app.ready_line()
    assert READY.fullmatch(line), line
    port = json.loads(line)["port"]
    assert port not in (0, occupied_port), "AIHUB_PORT must win over the file's port"
    base = f"http://127.0.0.1:{port}"
    assert httpx2.get(f"{base}/api/health", timeout=5).json() == {"ok": True}
    # The line comes after the lifespan startup, so the models are already loaded.
    bootstrap = httpx2.get(f"{base}/api/bootstrap", timeout=5)
    assert bootstrap.status_code == 200 and bootstrap.json()["default_model"]
    app.stop()
    assert app.stdout_rest() == "", "the ready line is the only thing on stdout"


def test_without_any_new_variable_the_service_path_is_unchanged(
    launch,
) -> None:
    """systemd starts the unit with stdin on /dev/null, which is at EOF from the first read."""
    port = free_port()
    app = launch(config_port=port)
    assert app.ready_line() == f'{{"event":"ready","port":{port}}}\n'
    health = f"http://127.0.0.1:{port}/api/health"
    assert httpx2.get(health, timeout=5).status_code == 200  # no cookie, no token
    time.sleep(1.5)  # a stdin watcher would have stopped the server long before this
    assert app.process.poll() is None, app.log()
    assert httpx2.get(health, timeout=5).status_code == 200
    app.stop()
    assert app.stdout_rest() == ""


@pytest.mark.parametrize("value", ["0", "true", ""])
def test_only_the_value_1_turns_the_stdin_watcher_on(launch, value: str) -> None:
    """Here stdin is at EOF from the first read, so a watcher that was on would stop the server at once."""
    app = launch(AIHUB_EXIT_ON_STDIN_EOF=value)
    base = f"http://127.0.0.1:{app.port()}"
    with pytest.raises(subprocess.TimeoutExpired):
        app.process.wait(timeout=1)
    assert httpx2.get(f"{base}/api/health", timeout=5).status_code == 200
    app.stop()


def close_stdin(app: Launch) -> None:
    """What the kernel does when the parent that owns the pipe dies."""
    assert app.process.stdin is not None
    app.process.stdin.close()


def interrupt(app: Launch) -> None:
    """What Ctrl+C sends to a server started in a terminal."""
    app.process.send_signal(signal.SIGINT)


def stop_mid_reply(
    launch,
    occupied_port: int,
    tmp_path: Path,
    stop,
    statuses: tuple[int, ...],
    forbidden: tuple[str, ...],
) -> None:
    """Stop a server the way the desktop shell runs it while a reply streams: the exit is clean and
    what had streamed is kept as a stopped reply."""
    app = launch(
        stdin=subprocess.PIPE,
        config_port=occupied_port,
        AIHUB_PORT="0",
        AIHUB_EXIT_ON_STDIN_EOF="1",
    )
    client = httpx2.Client(base_url=f"http://127.0.0.1:{app.port()}", timeout=30)
    with (
        client,
        client.stream("POST", "/api/chat", json={"text": "endless please"}) as response,
    ):
        lines = response.iter_lines()
        streamed = ""
        for line in lines:
            event = json.loads(line)
            streamed += event["d"] if event["t"] == "text" else ""
            if streamed.count("word") >= 3:
                break
        stopped_at = time.monotonic()
        stop(app)
        # Once the grace period is over the server drops the connection mid-stream. Reading stops at the
        # budget, or a server that held the connection for longer would still look as if it stopped in time.
        with contextlib.suppress(httpx2.RemoteProtocolError):
            for _ in lines:
                if time.monotonic() - stopped_at > STOP_BUDGET:
                    break
    status = app.process.wait(timeout=20)
    took = time.monotonic() - stopped_at
    assert took <= STOP_BUDGET, f"stopping took {took:.1f} s; log:\n{app.log()}"
    assert status in statuses, app.log()

    log = app.log()
    assert all(text not in log for text in forbidden), log
    store = Store(tmp_path / "state" / "chats.db")
    try:
        [chat] = store.list_chats()
        user, reply = store.messages(chat.id)
        assert user.content == "endless please"
        assert reply.status == "stopped"
        assert reply.content.startswith("word word word")
        assert len(reply.content) >= len(streamed.rstrip())
    finally:
        store.close()


def test_closing_stdin_stops_the_server_and_keeps_the_partial_reply(
    launch, occupied_port: int, tmp_path: Path
) -> None:
    stop_mid_reply(
        launch,
        occupied_port,
        tmp_path,
        close_stdin,
        statuses=(0, -signal.SIGTERM),
        forbidden=("Traceback", "Fatal Python error"),
    )


def test_sigint_mid_reply_keeps_the_partial_reply(
    launch, occupied_port: int, tmp_path: Path
) -> None:
    # SIGINT does not end the process the instant the shutdown is done, as SIGTERM does, so the request
    # uvicorn cancelled after the grace period logs its CancelledError. uvicorn.run did too.
    stop_mid_reply(
        launch,
        occupied_port,
        tmp_path,
        interrupt,
        statuses=(0,),
        forbidden=("KeyboardInterrupt", "Fatal Python error"),
    )


def test_closing_stdin_stops_an_idle_server_and_logs_why(launch) -> None:
    app = launch(stdin=subprocess.PIPE, AIHUB_PORT="0", AIHUB_EXIT_ON_STDIN_EOF="1")
    app.ready_line()
    close_stdin(app)
    assert app.process.wait(timeout=STOP_BUDGET) in (0, -signal.SIGTERM), app.log()
    log = app.log()
    assert "stdin closed, stopping" in log
    assert "Traceback" not in log and "Fatal Python error" not in log, log


def test_a_signal_still_stops_a_server_that_is_watching_stdin(
    launch, occupied_port: int
) -> None:
    app = launch(
        stdin=subprocess.PIPE,
        config_port=occupied_port,
        AIHUB_PORT="0",
        AIHUB_EXIT_ON_STDIN_EOF="1",
    )
    app.ready_line()
    app.stop()
    log = app.log()
    assert "Traceback" not in log and "Fatal Python error" not in log, log


def test_sigint_ends_the_server_cleanly(start) -> None:
    """Ctrl+C on the README's throwaway instance ended with status 0 and no traceback under uvicorn.run."""
    app = start(AIHUB_PORT="0")
    app.ready_line()
    interrupt(app)
    assert app.process.wait(timeout=20) == 0, app.log()
    log = app.log()
    assert "Traceback" not in log and "Fatal Python error" not in log, log
    assert app.stdout_rest() == ""


def test_a_taken_port_exits_3_without_a_ready_line(start, occupied_port: int) -> None:
    app = start(AIHUB_PORT=str(occupied_port))
    assert app.process.wait(timeout=10) == 3, app.log()
    assert app.stdout_rest() == ""
    assert "address already in use" in app.log()


def test_a_startup_failure_exits_3_without_a_ready_line(start, tmp_path: Path) -> None:
    (tmp_path / "state").mkdir()
    newer = sqlite3.connect(tmp_path / "state" / "chats.db")
    newer.execute("PRAGMA user_version = 99")  # a database from a newer version
    newer.close()
    app = start(AIHUB_PORT="0")
    assert app.process.wait(timeout=10) == 3, app.log()
    assert app.stdout_rest() == ""
    assert "newer than this app" in app.log()


def test_a_server_that_returns_without_starting_exits_3(launch) -> None:
    """uvicorn 0.54.0 exits 3 by itself when the bind or the lifespan fails (the two tests above).

    This pins our own exit status for a uvicorn whose Server.run returns without having started, as
    the startup of older releases did after a lifespan failure.
    """
    app = launch(args=("-c", RETURNS_WITHOUT_STARTING))
    assert app.process.wait(timeout=10) == 3, app.log()
    assert app.stdout_rest() == ""


def test_a_bad_port_variable_exits_1_with_a_message(start) -> None:
    app = start(AIHUB_PORT="not-a-port")
    assert app.process.wait(timeout=10) == 1, app.log()
    assert app.stdout_rest() == ""
    assert "aihub-web: " in app.log() and "port: Input should be" in app.log()


def test_a_launch_token_in_the_environment_guards_the_api(
    launch, occupied_port: int
) -> None:
    token = secrets.token_urlsafe(32)
    app = launch(config_port=occupied_port, AIHUB_PORT="0", AIHUB_LAUNCH_TOKEN=token)
    base = f"http://127.0.0.1:{app.port()}"
    assert httpx2.get(f"{base}/api/health", timeout=5).status_code == 401
    own = {"cookie": f"aihub_t={token}"}
    assert httpx2.get(f"{base}/api/health", headers=own, timeout=5).status_code == 200
    assert httpx2.get(f"{base}/", timeout=5).status_code == 200
    app.stop()


def test_a_cookie_that_is_not_text_is_refused_not_a_server_error(
    launch, occupied_port: int
) -> None:
    app = launch(config_port=occupied_port, AIHUB_PORT="0", AIHUB_LAUNCH_TOKEN="secret")
    request = (
        b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n"
        b"Cookie: aihub_t=\xff\xfe\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", app.port()), timeout=5) as conn:
        conn.sendall(request)
        status_line = conn.recv(64).split(b"\r\n", 1)[0]
    assert status_line == b"HTTP/1.1 401 Unauthorized", app.log()
    app.stop()
