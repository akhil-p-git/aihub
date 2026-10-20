"""Stopping the service in the middle of a reply must keep what had streamed, and must exit cleanly."""

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx2
from aihub_web.db import Store

ROOT = Path(__file__).parents[1]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def wait_until_up(url: str, process: subprocess.Popen) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        assert process.poll() is None, f"{url} exited early with {process.returncode}"
        try:
            if httpx2.get(url, timeout=1).status_code == 200:
                return
        except httpx2.HTTPError:
            time.sleep(0.05)
    raise AssertionError(f"{url} never came up")


def test_stopping_the_service_mid_reply_keeps_the_partial_reply(tmp_path: Path) -> None:
    fake_port, app_port = free_port(), free_port()
    fake = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tests" / "e2e" / "fake_providers.py"),
            str(fake_port),
        ]
    )
    config = tmp_path / "web.yaml"
    config.write_text(
        f"port: {app_port}\n"
        f'ollama: {{ url: "http://127.0.0.1:{fake_port}" }}\n'
        f'openrouter: {{ url: "http://127.0.0.1:{fake_port}/api/v1" }}\n'
    )
    log = (tmp_path / "app.log").open("w")
    app = subprocess.Popen(
        [sys.executable, "-m", "aihub_web"],
        env={
            "PATH": os.environ["PATH"],
            "PYTHONPATH": str(ROOT / "app"),
            "AIHUB_WEB_CONFIG": str(config),
            "AIHUB_STATE_DIR": str(tmp_path / "state"),
            "AIHUB_OPENROUTER_KEY": "test-key",
        },
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_until_up(f"http://127.0.0.1:{fake_port}/api/tags", fake)
        wait_until_up(f"http://127.0.0.1:{app_port}/api/health", app)

        client = httpx2.Client(base_url=f"http://127.0.0.1:{app_port}", timeout=30)
        with (
            client,
            client.stream(
                "POST", "/api/chat", json={"text": "endless please"}
            ) as response,
        ):
            lines = response.iter_lines()
            streamed = ""
            for line in lines:
                event = json.loads(line)
                streamed += event["d"] if event["t"] == "text" else ""
                if streamed.count("word") >= 3:
                    break
            app.send_signal(signal.SIGTERM)  # what `systemctl restart aihub-web` sends
            # Once the grace period is over the server drops the connection mid-stream, which a client
            # sees as an incomplete chunked read (the page shows "the connection dropped").
            with contextlib.suppress(httpx2.RemoteProtocolError):
                for _ in lines:
                    pass
        # uvicorn re-raises the signal it was stopped by, which systemd counts as a clean stop.
        assert app.wait(timeout=20) in (0, -signal.SIGTERM)
    finally:
        for process in (app, fake):
            if process.poll() is None:
                process.kill()
            process.wait()
        log.close()

    output = (tmp_path / "app.log").read_text()
    assert "Traceback" not in output, output

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
