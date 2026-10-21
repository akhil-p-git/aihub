"""Host for one sd-server engine, as the main process of aihub-images.service. Standard library only.

systemd hands the launcher the listening Unix socket as fd 3 and starts it on the first connection. The
engine is its child in the same private network namespace, so it has no network but `lo`. The launcher
refuses to run anywhere else, answers a fixed set of routes, starts the engine for a model on demand, keeps
at most one engine alive, and exits after 5 idle minutes. The next connection starts it again.

Each connection carries one request and one reply, both with "Connection: close". The routes are:

    GET  /launcher/status                       200 {engine, state, last_error}; never starts an engine
    POST /launcher/kill                         200 {killed}; SIGKILL to the engine, answered once it is gone
    POST /e/<model>/sdcpp/v1/img_gen            starts the engine for <model>, then forwards
    GET  /e/<model>/sdcpp/v1/jobs/<id>          forwarded when the engine for <model> is ready, else 410
    POST /e/<model>/sdcpp/v1/jobs/<id>/cancel   the same

Everything else is 404. A client's bytes are never passed on: the launcher reads the request and sends the
engine a new one, made of the method, a path it builds from the route and, for img_gen, the body it counted.
The engine's reply is checked as hard: one Content-Length, no chunking, a bounded head and body.

Errors have a JSON body {code, ...}: 400 bad_request, 404 not_found or unknown_model, 408 request_timeout,
411 length_required, 413 body_too_large, 415 unsupported_media_type, 431 head_too_large, 503 busy or
engine_failed_to_start (with last_error), 410 engine_gone (with last_error), 502 bad_engine_reply or
engine_unreachable, 504 engine_timeout.

The engine's [E] and [W] lines are kept (the last 20 are last_error) and the first 200 per start go to this
process's stderr, which systemd sends to the journal. Everything else it prints is dropped, because sd-server
logs prompts at [I]. It prints [E] on stderr and the rest on stdout, so both are read.

A start that fails (the engine exits or does not listen) leaves the launcher running in state "failed" and
refuses any new start for 30 seconds, so one click costs one CUDA initialisation. Exit status 78 means the
launcher would not run: it is not alone in a network namespace, it was not given exactly one socket, or
models.json or a timing variable is wrong.

The engine must not outlive the launcher. Under systemd, KillMode=control-group and PrivatePIDs=yes end it
with the unit; elsewhere PR_SET_PDEATHSIG ends it when the launcher dies.

AIHUB_IMAGES_<NAME>_S sets a timing in seconds, once, at start: IDLE, BACKOFF, START, GRACE, CLIENT and
ENGINE (the defaults are in TIMES). Tests use them so that they need not wait minutes. The unit sets none.
"""

import asyncio
import collections
import contextlib
import ctypes
import itertools
import json
import math
import os
import re
import signal
import socket
import sys
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

EX_CONFIG = 78
# A request head or an engine reply head, up to and including the blank line.
HEAD_MAX = 16 * 1024
BODY_MAX = 32 * 1024 * 1024
REPLY_MAX = 16 * 1024 * 1024
MAX_CONNECTIONS = 16
STREAM_LIMIT = 1024 * 1024
# Engine error lines kept for the status route.
RING = 20
# Characters kept of an engine line.
LINE_MAX = 1000
# Engine lines copied to the journal per start.
LOG_LINES_MAX = 200
# How long to wait for a SIGKILLed engine to be gone.
KILL_WAIT = 10.0
TIMES = {
    # No connection: stop the engine and exit.
    "IDLE": 300.0,
    # After a failed start: no new start.
    "BACKOFF": 30.0,
    # The engine must listen by then. It loads the model first.
    "START": 90.0,
    # From SIGTERM to SIGKILL.
    "GRACE": 5.0,
    # To send a request, and to take the reply.
    "CLIENT": 30.0,
    # One exchange with the engine.
    "ENGINE": 60.0,
}

MODEL = r"[a-z0-9-]{1,32}"
JOB = r"[A-Za-z0-9_-]{1,64}"
IMG_GEN = re.compile(rf"/e/({MODEL})/sdcpp/v1/img_gen")
JOB_STATUS = re.compile(rf"/e/({MODEL})/sdcpp/v1/jobs/({JOB})")
JOB_CANCEL = re.compile(rf"/e/({MODEL})/sdcpp/v1/jobs/({JOB})/cancel")
REQUEST_LINE = re.compile(r"([A-Z]+) (/[^ ]*) HTTP/1\.[01]")
STATUS_LINE = re.compile(r"HTTP/1\.[01] ([0-9]{3})(?: .*)?")
HEADER = re.compile(r"([!#$%&'*+.^_`|~0-9A-Za-z-]+):[ \t]*(.*?)[ \t]*")
# Control characters. A lone CR or LF is one, and only CRLF may end a line.
NOT_TEXT = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
MEDIA_TYPE = re.compile(
    r"[A-Za-z0-9!#$&^_.+-]+/[A-Za-z0-9!#$&^_.+-]+(?:[ \t]*;[ -~]*)?"
)
JSON_TYPE = re.compile(r"application/json(?:[ \t]*;.*)?", re.IGNORECASE)
VERBOSE_FLAG = re.compile(r"-v+|--verbose|--log-level(=.*)?")
LENGTH = re.compile(r"[0-9]{1,10}")

# prctl(PR_SET_PDEATHSIG, SIGKILL), looked up before any fork so that the child only has to call it.
_PRCTL = ctypes.CDLL(None, use_errno=True).prctl
_PRCTL.argtypes = [ctypes.c_int, ctypes.c_ulong]
_LAUNCHER_PID = os.getpid()


def log(message: str) -> None:
    print(f"launcher: {message}", file=sys.stderr, flush=True)


def refuse(message: str):
    log(f"refusing to start: {message}")
    sys.exit(EX_CONFIG)


# --- start-up checks ------------------------------------------------------------------------------------


def check_network() -> None:
    """Only `lo` may exist. Never socket.if_nameindex(): it needs AF_NETLINK, which the unit does not allow.

    In a unit with PrivateNetwork=yes, systemd 262 mounts a second, read-only sysfs over /sys, so this lists
    the unit's own namespace. Plain `unshare --net` does not: /sys still shows the host's interfaces.
    """
    try:
        interfaces = sorted(os.listdir("/sys/class/net"))
    except OSError as err:
        refuse(f"cannot list the network interfaces ({err}); is /sys mounted?")
    if interfaces != ["lo"]:
        refuse(
            "this is not a private network namespace (interfaces: "
            f"{', '.join(interfaces) or 'none'}); the engine must have no network"
        )


def take_socket() -> socket.socket:
    pid, count = os.environ.get("LISTEN_PID"), os.environ.get("LISTEN_FDS")
    if pid != str(os.getpid()) or count != "1":
        refuse(
            f"LISTEN_PID={pid}, LISTEN_FDS={count}: not one socket for pid {os.getpid()}"
        )
    try:
        sock = socket.socket(fileno=3)
    except OSError as err:
        refuse(f"fd 3 is not a socket ({err})")
    if (
        sock.family != socket.AF_UNIX
        or sock.type != socket.SOCK_STREAM
        or not sock.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
    ):
        refuse("fd 3 is not a listening unix stream socket")
    sock.set_inheritable(False)
    sock.setblocking(False)
    return sock


def load_times() -> dict[str, float]:
    times = dict(TIMES)
    for name in times:
        variable = f"AIHUB_IMAGES_{name}_S"
        if variable in os.environ:
            try:
                value = float(os.environ[variable])
            except ValueError:
                value = math.nan
            if not (math.isfinite(value) and value > 0):
                refuse(f"{variable} must be a positive number of seconds")
            times[name] = value
    return times


@dataclass(frozen=True)
class Config:
    engine: str
    common: list[str]
    models: dict[str, list[str]]
    host: str
    port: int

    def argv(self, model: str) -> list[str]:
        return [self.engine, *self.common, *self.models[model]]


def load_config(path: str) -> Config:
    def bad(why: str):
        refuse(f"models.json: {why}")

    def strings(value) -> bool:
        return isinstance(value, list) and all(
            isinstance(v, str) and "\0" not in v for v in value
        )

    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError) as err:
        bad(f"cannot read {path}: {err}")
    if not isinstance(data, dict):
        bad("the file must hold a JSON object")
    engine, common, models = (data.get(k) for k in ("engine", "common_args", "models"))
    if not isinstance(engine, str) or not engine or "\0" in engine:
        bad('"engine" must be the path of the engine')
    if not strings(common):
        bad('"common_args" must be a list of strings')
    if not isinstance(models, dict) or not models:
        bad('"models" must be an object with at least one model')
    where = {"--listen-ip", "--listen-port", "-l"}
    if "-l" in common:
        bad('"-l" in common_args: say "--listen-ip"')
    for model, args in models.items():
        if not re.fullmatch(MODEL, model):
            bad(f"model id {model!r} must match {MODEL}")
        if not strings(args):
            bad(f"{model}: the arguments must be a list of strings")
        if where & set(args):
            bad(f"{model}: only common_args may say where the engine listens")
    for arg in common + [a for args in models.values() for a in args]:
        if VERBOSE_FLAG.fullmatch(arg):
            bad(f"{arg} would make the engine log prompts")
    following = dict(itertools.pairwise(common))
    host, port = following.get("--listen-ip"), following.get("--listen-port", "")
    if host != "127.0.0.1":
        bad('common_args must hold "--listen-ip" "127.0.0.1"')
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 0 < int(port) < 65536:
        bad('common_args must hold "--listen-port" and a port number')
    return Config(engine, common, models, host, int(port))


# --- HTTP, in both directions ---------------------------------------------------------------------------


class Reject(Exception):
    """An answer for the client instead of going on."""

    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status, self.code = status, code


class Malformed(Exception):
    """A head that must not be passed on."""


class TooLarge(Malformed):
    """More than HEAD_MAX bytes before the blank line."""


class Hangup(Exception):
    """The peer closed before sending anything."""


class BadReply(Exception):
    """The engine's reply is not one the launcher will pass on."""


async def read_head(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """Read up to the blank line. Returns the head without it, and any bytes read beyond it."""
    buffer = bytearray()
    while True:
        chunk = await reader.read(HEAD_MAX + 1 - len(buffer))
        if not chunk:
            raise Malformed("closed inside the head") if buffer else Hangup()
        buffer += chunk
        end = buffer.find(b"\r\n\r\n")
        if end >= 0:
            if end + 4 > HEAD_MAX:
                raise TooLarge()
            return bytes(buffer[:end]), bytes(buffer[end + 4 :])
        if len(buffer) >= HEAD_MAX:
            raise TooLarge()


def split_head(head: bytes) -> tuple[str, list[tuple[str, str]]]:
    """The first line, and the headers as (lower-case name, value). No bare CR or LF, no folded lines."""
    try:
        first, *rest = head.decode("ascii").split("\r\n")
    except UnicodeDecodeError:
        raise Malformed("not ASCII") from None
    headers = []
    for line in rest:
        match = HEADER.fullmatch(line)
        if not match:
            raise Malformed("not a header line")
        headers.append((match[1].lower(), match[2]))
    if NOT_TEXT.search(first) or any(NOT_TEXT.search(v) for _, v in headers):
        raise Malformed("control character")
    return first, headers


def single(headers: list[tuple[str, str]], name: str) -> str | None:
    found = [value for key, value in headers if key == name]
    if len(found) > 1:
        raise Malformed(f"two {name} headers")
    return found[0] if found else None


@dataclass
class Request:
    method: str
    target: str
    length: int | None
    content_type: str | None
    # Bytes after the head: the start of the body, or further requests, which are ignored.
    leftover: bytes


async def read_request(reader: asyncio.StreamReader) -> Request:
    try:
        head, leftover = await read_head(reader)
        first, headers = split_head(head)
        line = REQUEST_LINE.fullmatch(first)
        if not line:
            raise Malformed("request line")
        # Only a Content-Length body is understood.
        if single(headers, "transfer-encoding") is not None:
            raise Malformed("transfer coding")
        length = single(headers, "content-length")
        if length is not None and not LENGTH.fullmatch(length):
            raise Malformed("content length")
        content_type = single(headers, "content-type")
    except TooLarge:
        raise Reject(431, "head_too_large") from None
    except Malformed:
        raise Reject(400, "bad_request") from None
    if length is not None and int(length) > BODY_MAX:
        raise Reject(413, "body_too_large")
    size = None if length is None else int(length)
    return Request(line[1], line[2], size, content_type, leftover)


async def read_body(reader: asyncio.StreamReader, req: Request) -> bytes:
    assert req.length is not None
    # Bytes beyond the length are ignored.
    body = req.leftover[: req.length]
    try:
        return body + await reader.readexactly(req.length - len(body))
    except asyncio.IncompleteReadError:
        raise Reject(400, "bad_request") from None


async def read_reply(reader: asyncio.StreamReader) -> tuple[int, str | None, bytes]:
    """The engine's reply as (status, content type, body). BadReply unless it is exactly what is accepted."""
    try:
        head, leftover = await read_head(reader)
        first, headers = split_head(head)
        length = single(headers, "content-length")
        content_type = single(headers, "content-type")
    except Hangup:
        raise EOFError("the engine closed the connection without a reply") from None
    except Malformed:
        raise BadReply() from None
    status = STATUS_LINE.fullmatch(first)
    if (
        not status
        or not 200 <= int(status[1]) <= 599
        or any(key == "transfer-encoding" for key, _ in headers)
        or length is None
        or not LENGTH.fullmatch(length)
        or int(length) > REPLY_MAX
        or (content_type is not None and not MEDIA_TYPE.fullmatch(content_type))
    ):
        raise BadReply()
    size = int(length)
    try:
        body = leftover[:size] + await reader.readexactly(max(0, size - len(leftover)))
    except asyncio.IncompleteReadError:
        raise BadReply() from None
    return int(status[1]), content_type, body


def encode(
    status: int, payload: dict | bytes, content_type: str | None = "application/json"
) -> bytes:
    if isinstance(payload, bytes):
        body = payload
    else:
        body = json.dumps(payload, separators=(",", ":")).encode()
    try:
        reason = HTTPStatus(status).phrase
    except ValueError:
        reason = "Status"
    head = f"HTTP/1.1 {status} {reason}\r\n"
    if content_type:
        head += f"Content-Type: {content_type}\r\n"
    head += f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    return head.encode() + body


# --- the engine -----------------------------------------------------------------------------------------


def die_with_the_launcher() -> None:
    """Runs in the child before it executes the engine: the kernel kills it when the launcher dies."""
    # PR_SET_PDEATHSIG is 1.
    if _PRCTL(1, int(signal.SIGKILL)) != 0 or os.getppid() != _LAUNCHER_PID:
        # No promise could be made, or the launcher is already gone.
        os._exit(1)


def printable(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace").rstrip("\r\n")
    return "".join(c if c.isprintable() else "?" for c in text)[:LINE_MAX]


def describe_exit(status: int) -> str:
    if status >= 0:
        return f"the engine exited with status {status}"
    try:
        return f"the engine was killed by signal {signal.Signals(-status).name}"
    except ValueError:
        return f"the engine was killed by signal {-status}"


@dataclass
class Engine:
    model: str
    proc: asyncio.subprocess.Process
    # Set when the process has exited and its output is read.
    done: asyncio.Event = field(default_factory=asyncio.Event)
    # We signalled it, so its exit is no error.
    asked_to_stop: bool = False
    # POST /launcher/kill did.
    killed_by_request: bool = False


class Host:
    def __init__(self, config: Config, times: dict[str, float], sock: socket.socket):
        self.config, self.times, self.sock = config, times, sock
        # idle, starting, ready or failed.
        self.state = "idle"
        # The model of the engine that is starting or ready.
        self.model: str | None = None
        self.engine: Engine | None = None
        self.errors: collections.deque[str] = collections.deque(maxlen=RING)
        self.logged = 0
        self.failed_at: float | None = None
        # One start or stop at a time.
        self.lock = asyncio.Lock()
        # Set unless a start is in the middle of creating the process.
        self.spawned = asyncio.Event()
        self.spawned.set()
        self.active = 0
        self.idle_since = time.monotonic()
        self.connections: set[asyncio.Task] = set()
        self.watchers: set[asyncio.Task] = set()
        self.stopping = asyncio.Event()

    def settle(self, state: str) -> None:
        self.state = state
        if state in ("idle", "failed"):
            self.model = self.engine = None

    # What the routes say about the engine
    def snapshot(self) -> dict:
        return {
            "engine": self.model,
            "state": self.state,
            "last_error": list(self.errors),
        }

    def gone(self) -> bytes:
        return encode(410, {"code": "engine_gone", "last_error": list(self.errors)})

    def failure(self) -> bytes:
        body = {"code": "engine_failed_to_start", "last_error": list(self.errors)}
        return encode(503, body)

    def note(self, line: str) -> None:
        """Say something of the launcher's own: in the error ring, and in the journal."""
        self.errors.append(f"[E] launcher: {line}")
        log(line)

    # Reading what the engine prints
    async def read_output(self, engine: Engine) -> None:
        while True:
            try:
                raw = await engine.proc.stdout.readline()
            except ValueError:
                # A line longer than the stream limit. Its start was dropped.
                continue
            if not raw:
                return
            # [I], [D] and [V] lines carry prompts and progress. They stay out of the journal.
            if not raw.startswith((b"[E] ", b"[W] ")):
                continue
            line = printable(raw)
            self.errors.append(line)
            if self.logged < LOG_LINES_MAX:
                self.logged += 1
                log(f"engine {engine.model}: {line}")
                if self.logged == LOG_LINES_MAX:
                    log("further engine lines are not logged; status has the last 20")

    async def watch(self, engine: Engine) -> None:
        reader = asyncio.create_task(self.read_output(engine))
        try:
            status = await engine.proc.wait()
            # What is left in the pipe. Something else may hold it open.
            with contextlib.suppress(TimeoutError, OSError):
                await asyncio.wait_for(reader, 1)
            if not engine.asked_to_stop:
                starting = self.engine is engine and self.state == "starting"
                before = " before it listened" if starting else ""
                self.note(describe_exit(status) + before)
            if self.engine is engine and self.state == "ready":
                self.settle("idle")
        finally:
            # Whatever went wrong above, whoever waits for the engine must be let go.
            engine.done.set()

    # Starting and stopping
    async def ensure(self, model: str) -> bytes | None:
        """Make the engine for `model` ready. None when it is, otherwise the 503 to answer with."""
        async with self.lock:
            engine = self.engine
            alive = engine is not None and not engine.done.is_set()
            if self.state == "ready" and alive and engine.model == model:
                return None
            if alive:
                # A request for another model. Never two engines at once.
                await self.stop(engine)
            since = (
                None if self.failed_at is None else time.monotonic() - self.failed_at
            )
            if since is not None and since < self.times["BACKOFF"]:
                return self.failure()
            return await self.start(model)

    async def spawn(self, model: str) -> Engine | None:
        argv = self.config.argv(model)
        env = {k: v for k, v in os.environ.items() if not k.startswith("LISTEN_")}
        self.spawned.clear()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
                limit=64 * 1024,
                preexec_fn=die_with_the_launcher,
            )
        except (OSError, ValueError) as err:
            self.note(f"cannot run {argv[0]}: {err}")
            return None
        finally:
            self.spawned.set()
        engine = self.engine = Engine(model, proc)
        watcher = asyncio.create_task(self.watch(engine))
        self.watchers.add(watcher)
        watcher.add_done_callback(self.watchers.discard)
        return engine

    async def start(self, model: str) -> bytes | None:
        self.errors.clear()
        self.logged = 0
        self.state, self.model, self.engine = "starting", model, None
        log(f"starting the engine for {model}")
        engine = await self.spawn(model)
        if engine is None:
            return self.fail()
        # If the launcher is told to stop meanwhile, run() stops this engine too.
        if await self.listening(engine) and not engine.done.is_set():
            self.failed_at = None
            self.settle("ready")
            where = f"{self.config.host}:{self.config.port}"
            log(f"the engine for {model} listens on {where}")
            return None
        if not engine.done.is_set():
            # Alive, but it never listened.
            waited = f"{self.times['START']:g} s"
            self.note(f"the engine did not listen within {waited} and was killed")
            engine.asked_to_stop = True
            with contextlib.suppress(ProcessLookupError):
                engine.proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(engine.done.wait(), KILL_WAIT)
        if engine.killed_by_request:
            # Not a failure of the engine, so no backoff.
            self.settle("idle")
            return self.failure()
        return self.fail()

    def fail(self) -> bytes:
        self.failed_at = time.monotonic()
        self.settle("failed")
        return self.failure()

    async def listening(self, engine: Engine) -> bool:
        """Wait for the engine to accept a connection. sd-server listens only after it has loaded the model."""
        address = (self.config.host, self.config.port)
        deadline = time.monotonic() + self.times["START"]
        while time.monotonic() < deadline and not engine.done.is_set():
            try:
                async with asyncio.timeout(1):
                    _, writer = await asyncio.open_connection(*address)
            except (OSError, TimeoutError):
                await asyncio.sleep(0.05)
                continue
            writer.close()
            return True
        return False

    async def stop(self, engine: Engine) -> None:
        """SIGTERM, then SIGKILL after the grace period."""
        engine.asked_to_stop = True
        if not engine.done.is_set():
            with contextlib.suppress(ProcessLookupError):
                engine.proc.terminate()
            try:
                await asyncio.wait_for(engine.done.wait(), self.times["GRACE"])
            except TimeoutError:
                grace = f"{self.times['GRACE']:g} s"
                log(f"the engine for {engine.model} ignored SIGTERM for {grace}")
                with contextlib.suppress(ProcessLookupError):
                    engine.proc.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(engine.done.wait(), KILL_WAIT)
        if self.engine is engine:
            self.settle("idle")

    async def kill(self) -> bool:
        # A start that is creating the process has an engine to kill in a moment.
        await self.spawned.wait()
        engine = self.engine
        if engine is None or engine.done.is_set():
            return False
        engine.asked_to_stop = engine.killed_by_request = True
        with contextlib.suppress(ProcessLookupError):
            engine.proc.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(engine.done.wait(), KILL_WAIT)
        return True

    # Talking to the engine
    async def exchange(
        self, method: str, path: str, body: bytes | None
    ) -> tuple[int, str | None, bytes]:
        """Send the engine one request, built here, and return its checked reply."""
        address = (self.config.host, self.config.port)
        lines = [f"{method} {path} HTTP/1.1", f"Host: {address[0]}:{address[1]}"]
        if body is not None:
            lines += ["Content-Type: application/json", f"Content-Length: {len(body)}"]
        elif method == "POST":
            lines.append("Content-Length: 0")
        lines.append("Connection: close")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode() + (body or b"")
        async with asyncio.timeout(self.times["ENGINE"]):
            reader, writer = await asyncio.open_connection(*address, limit=STREAM_LIMIT)
            try:
                writer.write(request)
                await writer.drain()
                return await read_reply(reader)
            finally:
                writer.close()

    async def forward(
        self, method: str, path: str, body: bytes | None, engine: Engine
    ) -> bytes:
        try:
            status, content_type, payload = await self.exchange(method, path, body)
        except BadReply:
            return encode(502, {"code": "bad_engine_reply"})
        except TimeoutError:
            return encode(504, {"code": "engine_timeout"})
        except (OSError, EOFError):
            # The engine closed or refused the connection. Has it died?
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(engine.done.wait(), 1)
            if engine.done.is_set():
                return self.gone()
            problem = {"code": "engine_unreachable", "last_error": list(self.errors)}
            return encode(502, problem)
        return encode(status, payload, content_type)

    # One request
    async def from_client(self, awaitable):
        """Reading the request is the only thing the client has a time limit for."""
        try:
            async with asyncio.timeout(self.times["CLIENT"]):
                return await awaitable
        except TimeoutError:
            raise Reject(408, "request_timeout") from None

    def route(self, req: Request) -> tuple:
        if req.method == "GET" and req.target == "/launcher/status":
            return ("status",)
        if req.method == "POST" and req.target == "/launcher/kill":
            return ("kill",)
        for method, pattern, name in (
            ("POST", IMG_GEN, "img_gen"),
            ("GET", JOB_STATUS, "poll"),
            ("POST", JOB_CANCEL, "cancel"),
        ):
            match = pattern.fullmatch(req.target)
            if match and req.method == method:
                if match[1] not in self.config.models:
                    raise Reject(404, "unknown_model")
                return (name, *match.groups())
        raise Reject(404, "not_found")

    async def img_gen(self, reader, req: Request, model: str) -> bytes:
        if req.length is None:
            raise Reject(411, "length_required")
        if req.content_type is None or not JSON_TYPE.fullmatch(req.content_type):
            raise Reject(415, "unsupported_media_type")
        body = await self.from_client(read_body(reader, req))
        if failure := await self.ensure(model):
            return failure
        return await self.forward("POST", "/sdcpp/v1/img_gen", body, self.engine)

    async def job(self, name: str, model: str, job: str) -> bytes:
        engine = self.engine
        # A job is only asked about at the engine that made it, and only while that engine is up.
        if self.state != "ready" or engine is None or engine.model != model:
            return self.gone()
        if engine.done.is_set():
            return self.gone()
        if name == "poll":
            return await self.forward("GET", f"/sdcpp/v1/jobs/{job}", None, engine)
        path = f"/sdcpp/v1/jobs/{job}/cancel"
        return await self.forward("POST", path, None, engine)

    async def answer(self, reader: asyncio.StreamReader) -> bytes | None:
        """Read one request and return the whole reply. None: the client left without a word."""
        try:
            req = await self.from_client(read_request(reader))
            name, *ids = self.route(req)
            if name == "status":
                return encode(200, self.snapshot())
            if name == "kill":
                return encode(200, {"killed": await self.kill()})
            if name == "img_gen":
                return await self.img_gen(reader, req, *ids)
            return await self.job(name, *ids)
        except Reject as err:
            return encode(err.status, {"code": err.code})
        except Hangup:
            return None

    async def serve(self, conn: socket.socket, busy: bool) -> None:
        try:
            reader, writer = await asyncio.open_unix_connection(
                sock=conn, limit=STREAM_LIMIT
            )
        except OSError:
            conn.close()
            self.finished(busy)
            return
        try:
            try:
                if busy:
                    reply = encode(503, {"code": "busy"})
                else:
                    reply = await self.answer(reader)
            except (OSError, EOFError):
                reply = None
            if reply is not None:
                writer.write(reply)
                async with asyncio.timeout(self.times["CLIENT"]):
                    await writer.drain()
                writer.write_eof()
        except (OSError, TimeoutError):
            pass
        finally:
            writer.close()
            self.finished(busy)

    def finished(self, busy: bool) -> None:
        # A connection turned away was never counted.
        if not busy:
            self.active -= 1
        self.idle_since = time.monotonic()

    def accept(self) -> None:
        """The listening socket is readable. The connection is counted in the same step as the accept."""
        try:
            conn, _ = self.sock.accept()
        except (BlockingIOError, InterruptedError):
            return
        except OSError as err:
            log(f"accept failed: {err}")
            return
        conn.setblocking(False)
        # The decision is made here, from the connections being served, not in the task that answers.
        busy = self.active >= MAX_CONNECTIONS
        if not busy:
            self.active += 1
        task = asyncio.create_task(self.serve(conn, busy))
        self.connections.add(task)
        task.add_done_callback(self.connections.discard)

    # The whole life of the process
    def stop_listening(self) -> None:
        """Let go of the socket. systemd holds it open, so what waits in it is served by the next launcher."""
        if not self.stopping.is_set():
            asyncio.get_running_loop().remove_reader(self.sock.fileno())
            self.sock.close()
            self.stopping.set()

    async def watch_idle(self) -> None:
        tick = min(1.0, self.times["IDLE"] / 4)
        while True:
            await asyncio.sleep(tick)
            quiet = time.monotonic() - self.idle_since
            if self.active == 0 and quiet >= self.times["IDLE"]:
                log(f"idle for {self.times['IDLE']:g} s, stopping")
                # In the same step as the check, so that nothing can be accepted in between.
                self.stop_listening()
                return

    def on_signal(self) -> None:
        if not self.stopping.is_set():
            log("told to stop")
        self.stop_listening()

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        loop.add_reader(self.sock.fileno(), self.accept)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, self.on_signal)
        idle = asyncio.create_task(self.watch_idle())
        log(f"listening; models: {', '.join(self.config.models)}")
        await self.stopping.wait()
        idle.cancel()
        for task in list(self.connections):
            task.cancel()
        await asyncio.gather(*self.connections, return_exceptions=True)
        if self.engine and not self.engine.done.is_set():
            await self.stop(self.engine)
        return 0


def main() -> None:
    if len(sys.argv) != 2:
        refuse("usage: launcher.py MODELS_JSON")
    check_network()
    sock = take_socket()
    times = load_times()
    config = load_config(sys.argv[1])
    sys.exit(asyncio.run(Host(config, times, sock).run()))


if __name__ == "__main__":
    main()
