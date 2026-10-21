"""Shared test doubles: model data captured from real servers, and a network stub that serves it."""

import asyncio
import base64
import contextlib
import itertools
import json
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import date
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import httpx2
from aihub_web.catalog import Catalog
from aihub_web.chat import ChatService
from aihub_web.config import Config
from aihub_web.db import Store
from aihub_web.providers import Finish, Text

FIXTURES = Path(__file__).parent / "fixtures"
OLLAMA = json.loads((FIXTURES / "ollama_models.json").read_text())
OPENROUTER = json.loads((FIXTURES / "openrouter_models.json").read_text())
TODAY = date(2026, 10, 20)
KEYS = {"AIHUB_OPENROUTER_KEY": "sk-or-test", "AIHUB_ANTHROPIC_KEY": "sk-ant-test"}


def handler_for(
    *,
    ollama_up: bool = True,
    openrouter_up: bool = True,
    calls: list | None = None,
    ollama: "FakeOllama | None" = None,
):
    """A MockTransport handler for Ollama's model endpoints and OpenRouter's public model list.

    With `ollama`, the endpoints that show and unload the loaded models go to that stand-in too."""

    def handle(request: httpx2.Request) -> httpx2.Response:
        if calls is not None:
            calls.append(request.url.path)
        if request.url.host == "127.0.0.1":
            if not ollama_up:
                raise httpx2.ConnectError("refused", request=request)
            if ollama is not None and request.url.path in ("/api/ps", "/api/generate"):
                return ollama.handle(request)
            if request.url.path == "/api/tags":
                return httpx2.Response(200, json={"models": OLLAMA["tags"]})
            if request.url.path == "/api/show":
                return httpx2.Response(
                    200, json=OLLAMA["show"][json.loads(request.content)["model"]]
                )
        if request.url.host == "openrouter.ai" and request.url.path == "/api/v1/models":
            if not openrouter_up:
                raise httpx2.ConnectError("offline", request=request)
            return httpx2.Response(200, json={"data": OPENROUTER})
        return httpx2.Response(404)

    return handle


HANG = object()


class FakeProvider:
    """Plays a script as a provider's events.

    Items are events to yield, exceptions to raise, callables to run (for side effects such as
    advancing a clock), or HANG to wait until cancelled.
    """

    def __init__(self, *script: object) -> None:
        self.script = list(script)
        self.requests: list = []

    async def stream(self, req):
        self.requests.append(req)
        for item in list(self.script):
            if item is HANG:
                await asyncio.Event().wait()
            elif isinstance(item, BaseException):
                raise item
            elif callable(item):
                item()
            else:
                yield item


class Clock:
    """A hand-wound clock, for both wall time and monotonic time."""

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float = 0, *, days: float = 0) -> None:
        self.now += seconds + days * 86_400


async def make_service(tmp_path: Path, *, clock: "Clock | None" = None, **config):
    """A ChatService over a real store and the stubbed model catalog, with a FakeProvider per provider."""
    providers = {
        "openrouter": FakeProvider(Text("ok"), Finish("stop")),
        "ollama": FakeProvider(Text("ok"), Finish("stop")),
        "anthropic": FakeProvider(Text("ok"), Finish("stop")),
    }
    cfg = Config(state_dir=tmp_path, **config)
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler_for()))
    store = Store(tmp_path / "chats.db")
    catalog = Catalog(cfg, client, environ=KEYS, today=lambda: TODAY)
    await catalog.refresh()
    service = ChatService(
        cfg, store, catalog, providers, today=lambda: TODAY, monotonic=clock or Clock()
    )
    return service, store, providers


SHAPE_SIZES = {"square": (1024, 1024), "wide": (1024, 576), "tall": (576, 1024)}


def webp(shape: str = "square") -> bytes:
    """A small real WebP of that shape, as the engine writes it with metadata off: one lossy VP8 chunk.

    ffmpeg's libwebp encoder wrote these (cwebp is not installed); its RIFF layout is the same as what
    WebPEncodeRGB returns, which is all sd-server writes when embed_image_metadata is false.
    """
    return (FIXTURES / "images" / f"{shape}.webp").read_bytes()


def engine_errors(scenario: str) -> list[str]:
    """Real [E]/[W] lines from the image lab's logs (see engine-errors.json for where each came from)."""
    return json.loads((FIXTURES / "images" / "engine-errors.json").read_text())[
        scenario
    ]["lines"]


# What an image job's script holds: engine replies to polls, in order, the last one repeating. HOLD keeps
# answering with the previous reply until the test calls engine.release(); a callable is given the job's
# img_gen request body and returns the reply (the finished image has to match the size asked for).
HOLD = object()


def queued(position: int = 1) -> dict:
    return {
        "status": "queued",
        "queue_position": position,
        "result": None,
        "error": None,
    }


def generating(step: int | None = None, total: int | None = None) -> dict:
    job = {"status": "generating", "queue_position": 0, "result": None, "error": None}
    if step is not None:
        # The engine attaches a PNG of the current preview frame; the client has no use for it.
        preview = {
            "pass": 1,
            "step": step,
            "total_steps": total,
            "b64_json": "iVBORw0KGgo=",
        }
        job["preview"] = preview
    return job


def completed(image: bytes) -> dict:
    return {
        "status": "completed",
        "queue_position": 0,
        "result": {
            "output_format": "webp",
            "images": [{"index": 0, "b64_json": base64.b64encode(image).decode()}],
        },
        "error": None,
    }


def failed(
    code: str = "generation_failed", message: str = "generate_image returned no results"
) -> dict:
    return {
        "status": "failed",
        "queue_position": 0,
        "result": None,
        "error": {"code": code, "message": message},
    }


def finished(body: dict) -> dict:
    """The image the engine would make for this request: the fixture of the shape that matches its size."""
    size = (body["width"], body["height"])
    shape = next(
        (name for name, wanted in SHAPE_SIZES.items() if wanted == size), "square"
    )
    return completed(webp(shape))


def quick_job(steps: int) -> list:
    return [queued(1), *(generating(n, steps) for n in range(1, steps + 1)), finished]


class FakeEngine:
    """The image engine launcher's protocol, scripted. One instance serves both ways in: transport() for
    httpx2.MockTransport and serve_engine() for a Unix socket in a thread, so a test can use whichever it needs.

    Every request is recorded in `calls`. The jobs follow `scripts` (one list per job, first submitted first);
    with none left a job runs the standard steps, and ends with the image for the size that was asked for.
    """

    def __init__(self, *, model: str | None = "zimage") -> None:
        self.models = {"zimage", "klein4b"}
        self.engine = model  # the model that is loaded, as GET /launcher/status says
        self.state = "ready" if model else "idle"
        self.last_error: list[str] = []
        self.scripts: list[list] = []
        self.submit_response: tuple[int, object] | None = (
            None  # forces the answer to img_gen
        )
        self.submit_delay = (
            0.0  # seconds the launcher spends loading the model before answering
        )
        self.calls: list[tuple[str, str]] = []
        self.headers: list[dict[str, str]] = []
        self.submitted: list[dict] = []
        self.kills = 0
        self.cancels: list[str] = []
        self._jobs: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._permits = 0
        self._lock = threading.Lock()

    def release(self, count: int = 1) -> None:
        """Let the scripts move past `count` HOLDs."""
        with self._lock:
            self._permits += count

    def count(self, method: str, suffix: str) -> int:
        return sum(1 for m, path in self.calls if m == method and path.endswith(suffix))

    def delay_for(self, path: str) -> float:
        return self.submit_delay if path.endswith("/img_gen") else 0.0

    def handle(
        self, method: str, path: str, headers: dict[str, str], raw: bytes
    ) -> tuple[int, object]:
        with self._lock:
            self.calls.append((method, path))
            self.headers.append(headers)
            if (method, path) == ("GET", "/launcher/status"):
                return 200, {
                    "engine": self.engine,
                    "state": self.state,
                    "last_error": self.last_error,
                }
            if (method, path) == ("POST", "/launcher/kill"):
                killed = self.engine is not None
                self.kills += 1
                self.engine, self.state = None, "idle"
                return 200, {"killed": killed}
            route = re.fullmatch(
                r"/e/([^/]+)/sdcpp/v1/(img_gen|jobs/([^/]+)(/cancel)?)", path
            )
            if not route:
                return 404, {"error": "not found"}
            model, _, job_id, cancel = route.groups()
            if job_id is None:
                return self._submit(model, raw)
            if self.engine != model:
                return 410, {"code": "engine_gone", "last_error": self.last_error}
            return self._cancel(job_id) if cancel else self._poll(job_id)

    def _submit(self, model: str, raw: bytes) -> tuple[int, object]:
        if model not in self.models:
            return 404, {"code": "unknown_model"}
        if self.submit_response:
            return self.submit_response
        body = json.loads(raw)
        self.submitted.append(body)
        steps = body.get("sample_params", {}).get("sample_steps", 4)
        job_id = f"job_{next(self._ids):08x}"
        script = self.scripts.pop(0) if self.scripts else quick_job(steps)
        self._jobs[job_id] = {
            "script": script,
            "at": 0,
            "last": None,
            "body": body,
            "cancelled": False,
        }
        self.engine, self.state = model, "ready"
        return 202, {
            "id": job_id,
            "kind": "img_gen",
            "status": "queued",
            "created": 1,
            "poll_url": f"/sdcpp/v1/jobs/{job_id}",
        }

    def _poll(self, job_id: str) -> tuple[int, object]:
        job = self._jobs.get(job_id)
        if job is None:
            return 404, {"error": "job not found"}
        if job["cancelled"]:
            return 200, {
                "status": "cancelled",
                "queue_position": 0,
                "result": None,
                "error": {"code": "cancelled", "message": "job cancelled by client"},
            }
        script = job["script"]
        item = script[min(job["at"], len(script) - 1)]
        if item is HOLD:
            if not self._permits:
                return 200, job["last"] or generating()
            self._permits -= 1
            job["at"] += 1
            return self._poll(job_id)
        reply = item(job["body"]) if callable(item) else item
        if job["at"] < len(script) - 1:
            job["at"] += 1
        job["last"] = reply
        if isinstance(
            reply, tuple
        ):  # (status, body): an HTTP-level answer such as a 410
            return reply
        return 200, reply

    def _cancel(self, job_id: str) -> tuple[int, object]:
        job = self._jobs.get(job_id)
        if job is None:
            return 404, {"error": "job not found"}
        self.cancels.append(job_id)
        if (job["last"] or {}).get("status") == "generating":
            return 409, {
                "error": "job is currently generating and cannot be interrupted yet"
            }
        job["cancelled"] = True
        return 200, {"status": "cancelled"}

    def transport(self) -> httpx2.MockTransport:
        async def respond(request: httpx2.Request) -> httpx2.Response:
            if delay := self.delay_for(request.url.path):
                await asyncio.sleep(delay)
            status, payload = self.handle(
                request.method, request.url.path, dict(request.headers), request.content
            )
            return httpx2.Response(status, json=payload)

        return httpx2.MockTransport(respond)


@contextlib.contextmanager
def serve_engine(engine: FakeEngine, path: str) -> Iterator[FakeEngine]:
    """Serve the engine on a Unix socket in a thread, for the code that has to use a real socket."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = (
            "HTTP/1.0"  # one request per connection, as the launcher does it
        )

        def serve(self) -> None:
            raw = self.rfile.read(int(self.headers.get("content-length") or 0))
            if delay := engine.delay_for(self.path):
                time.sleep(delay)
            headers = {name.lower(): value for name, value in self.headers.items()}
            status, payload = engine.handle(self.command, self.path, headers, raw)
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = serve

        def log_message(self, *args) -> None:
            pass

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    server = Server(path, Handler)
    os.chmod(path, 0o600)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield engine
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class FakeOllama:
    """Ollama's /api/ps and /api/generate: which models are loaded, and unloading them with keep_alive 0."""

    def __init__(
        self,
        *resident: str,
        stubborn: tuple[str, ...] = (),
        size_vram: int = 5_000_000_000,
    ) -> None:
        self.resident = [
            {"name": name, "model": name, "size": size_vram, "size_vram": size_vram}
            for name in resident
        ]
        self.stubborn = set(
            stubborn
        )  # models that stay loaded when asked to leave, like one mid-reply
        self.calls: list[tuple[str, str, object]] = []

    def handle(self, request: httpx2.Request) -> httpx2.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        if request.url.path == "/api/ps":
            return httpx2.Response(200, json={"models": self.resident})
        if body.get("keep_alive") == 0 and body["model"] not in self.stubborn:
            self.resident = [m for m in self.resident if m["name"] != body["model"]]
        return httpx2.Response(
            200, json={"model": body["model"], "done": True, "done_reason": "unload"}
        )

    def posts(self) -> list[object]:
        return [body for method, path, body in self.calls if method == "POST"]


HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
fcntl.flock(fd, fcntl.LOCK_EX if sys.argv[2] == "exclusive" else fcntl.LOCK_SH)
print("locked", flush=True)
sys.stdin.read()  # holds the lock until stdin closes or the process is killed
"""


@contextlib.contextmanager
def holding(directory: Path, mode: str = "exclusive") -> Iterator[subprocess.Popen]:
    """Another process that holds the GPU lock on `directory`, like a second aihub or the bench script."""
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", HOLDER, str(directory), mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "locked"
        yield child
    finally:
        child.kill()
        child.wait()
        for pipe in (child.stdin, child.stdout):
            if pipe:
                pipe.close()
