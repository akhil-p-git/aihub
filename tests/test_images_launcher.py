"""The image engine launcher (systemd/images/launcher.py) against a stand-in engine.

Every launcher runs in its own user, network and mount namespace (`unshare --user --map-root-user --net
--mount`) with a fresh sysfs and `lo` up. That is the view of a unit with PrivateNetwork=yes: systemd 262
mounts a second, read-only sysfs over /sys, so /sys/class/net lists only `lo` (docs/adr/0001-image-engine.md).
Plain `unshare --net` is not enough, because /sys would still show the host's interfaces. The listening
socket arrives as fd 3 with LISTEN_PID and LISTEN_FDS set, as socket activation delivers it. A few tests
also give the launcher a PID namespace (PrivatePIDs=yes), where it is PID 1 and a signal without a handler
does nothing.

The tests use only the standard library and pytest, so `python3 -m pytest` runs them with either interpreter.
"""

import base64
import hashlib
import importlib.util
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
LAUNCHER = ROOT / "systemd" / "images" / "launcher.py"
SHIPPED_MODELS = ROOT / "systemd" / "images" / "models.json"
STAND_IN = ROOT / "tests" / "fixtures" / "images-engine" / "fake_sd_server.py"
SYSTEM_PYTHON = "/usr/bin/python3"  # what the unit runs
INTERPRETERS = list(
    {os.path.realpath(p): p for p in (sys.executable, SYSTEM_PYTHON)}.values()
)

HEAD_MAX = 16 * 1024
BODY_MAX = 32 * 1024 * 1024
UNSHARE = ["unshare", "--user", "--map-root-user", "--net", "--mount"]
PID_NAMESPACE = ["--pid", "--fork", "--mount-proc", "--kill-child"]
# What the launcher sends the engine for each route, to the byte.
CANON_HOST = "Host: 127.0.0.1:18234"

MODEL_ARGS = {
    "zimage": ["--diffusion-model", "z-image-turbo/z_image_turbo-Q5_0.gguf"],
    "klein4b": ["--diffusion-model", "flux2-klein-4b/flux-2-klein-4b-Q8_0.gguf"],
    "slowload": ["--standin-start", "slow=1.5"],
    "exit3": ["--standin-start", "exit3"],
    "crashes": ["--standin-start", "crash"],
    "hangs": ["--standin-start", "hang"],
    "stubborn": ["--standin-start", "stubborn"],
    "noisy": ["--standin-start", "noisy"],
}


def wait_for(predicate, what, *, timeout=10.0, rig=None):
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            log = f"\nlauncher log:\n{rig.log()}" if rig else ""
            raise AssertionError(f"gave up after {timeout} s waiting for {what}{log}")
        time.sleep(0.02)


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes
    raw: bytes

    def json(self):
        return json.loads(self.body)


def parse_reply(raw: bytes) -> Reply:
    if not raw:
        return Reply(0, {}, b"", raw)
    head, _, body = raw.partition(b"\r\n\r\n")
    status_line, *header_lines = head.decode("latin-1").split("\r\n")
    headers = {}
    for line in header_lines:
        name, _, value = line.partition(":")
        headers[name.lower()] = value.strip()
    return Reply(int(status_line.split(" ")[1]), headers, body, raw)


def exchange(path: str, data: bytes, *, timeout: float = 15, half_close=False) -> Reply:
    """Send raw bytes to the socket and read until the launcher closes it."""
    with socket.socket(socket.AF_UNIX) as conn:
        conn.settimeout(timeout)
        conn.connect(path)
        try:
            conn.sendall(data)
            if half_close:
                conn.shutdown(socket.SHUT_WR)
        except (BrokenPipeError, ConnectionResetError):
            pass  # answered before the end of the request; the reply is still readable
        raw = b""
        while True:
            try:
                chunk = conn.recv(1 << 16)
            except ConnectionResetError:
                break
            if not chunk:
                break
            raw += chunk
    return parse_reply(raw)


def request(method, target, body=b"", headers=(), version="HTTP/1.1") -> bytes:
    lines = [f"{method} {target} {version}", "Host: engine", *headers]
    if (body or method == "POST") and not any(
        h.lower().startswith("content-length") for h in headers
    ):
        lines.append(f"Content-Length: {len(body)}")
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


def stand_in_processes(marker: str) -> list[int]:
    """Live stand-in engines (not zombies) whose command line names this rig's record directory."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode(errors="replace")
            state = (entry / "stat").read_text().rpartition(")")[2].split()[0]
        except OSError:
            continue
        if marker in cmdline and "fake_sd_server" in cmdline and state != "Z":
            found.append(int(entry.name))
    return found


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except OSError:
        return False
    return state != "Z"


def cpu_ticks(pid: int) -> int:
    """User plus system CPU time of a process, in clock ticks."""
    fields = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()
    return int(fields[11]) + int(fields[12])


def open_request(rig, model, prompt="a red fox") -> socket.socket:
    """Connect and send a whole img_gen request, and leave the reply unread."""
    body = json.dumps({"prompt": prompt, "width": 64, "height": 64}).encode()
    conn = socket.socket(socket.AF_UNIX)
    conn.settimeout(15)
    conn.connect(rig.sock_path)
    headers = ("Content-Type: application/json",)
    conn.sendall(request("POST", f"/e/{model}/sdcpp/v1/img_gen", body, headers))
    return conn


def read_reply(conn: socket.socket) -> Reply:
    raw = b""
    while chunk := conn.recv(1 << 16):
        raw += chunk
    conn.close()
    return parse_reply(raw)


class Rig:
    """One launcher in its namespaces, the socket it was activated with, and the stand-in's records."""

    def __init__(
        self,
        base: Path,
        *,
        models=None,
        engine=str(STAND_IN),
        models_file: Path | None = None,
        env=None,
        python=sys.executable,
        namespaces=True,
        pid_namespace=False,
        listen_pid: str | None = "self",
        listen_fds: str | None = "1",
        fd3="listener",
        nofile: int | None = None,
        start=True,
    ):
        base.mkdir(parents=True, exist_ok=True)
        self.base = base
        self.engine_dir = base / "engine"
        self.engine_dir.mkdir()
        self.env = env or {}
        self.python = python
        self.namespaces = namespaces
        self.pid_namespace = pid_namespace
        self.listen_pid = listen_pid
        self.listen_fds = listen_fds
        self.fd3 = fd3
        self.nofile = nofile
        # A unix socket path is limited to 107 bytes, which pytest's tmp_path can exceed.
        self.sock_dir = Path(tempfile.mkdtemp(prefix="img-"))
        self.sock_path = str(self.sock_dir / "engine.sock")
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(self.sock_path)
        self.listener.listen(32)
        self.models_path = models_file or base / "models.json"
        if models_file is None:
            self.models_path.write_text(
                json.dumps(
                    {
                        "engine": engine,
                        "common_args": [
                            "--listen-ip",
                            "127.0.0.1",
                            "--listen-port",
                            "18234",
                            "--offload-to-cpu",
                            "--max-vram",
                            "3",
                            "--standin-dir",
                            str(self.engine_dir),
                        ],
                        "models": models or MODEL_ARGS,
                    },
                    indent=2,
                )
            )
        self.log_path = base / "launcher.log"
        self.proc: subprocess.Popen | None = None
        if start:
            self.start()

    def start(self) -> None:
        script = "exec 3<&0 </dev/null; "
        if self.nofile is not None:
            script += f"ulimit -n {self.nofile}; "
        if self.namespaces:
            script += "mount -t sysfs sysfs /sys && ip link set lo up || exit 70; "
        if self.listen_pid == "self":
            script += "export LISTEN_PID=$$; "
        elif self.listen_pid is not None:
            script += f"export LISTEN_PID={self.listen_pid}; "
        if self.listen_fds is not None:
            script += f"export LISTEN_FDS={self.listen_fds}; "
        script += 'exec "$@"'
        argv = [
            *(UNSHARE if self.namespaces else []),
            *(PID_NAMESPACE if self.pid_namespace else []),
            "sh",
            "-c",
            script,
            "sh",
            self.python,
            "-I",
            str(LAUNCHER),
            str(self.models_path),
        ]
        stdin = self.listener
        if self.fd3 == "file":
            stdin = os.open(self.models_path, os.O_RDONLY)
        elif self.fd3 == "connected":
            stdin, self._peer = socket.socketpair()
        elif self.fd3 == "tcp":
            stdin = socket.socket()
            stdin.bind(("127.0.0.1", 0))
            stdin.listen(1)
        with self.log_path.open("ab") as log:
            self.proc = subprocess.Popen(
                argv,
                stdin=stdin,
                stdout=subprocess.DEVNULL,
                stderr=log,
                env={"PATH": os.environ["PATH"], **self.env},
                cwd=self.base,
                start_new_session=True,
            )
        # The child has its own copy of whatever it was given in place of the listener.
        if isinstance(stdin, int):
            os.close(stdin)
        elif stdin is not self.listener:
            stdin.close()

    # What the launcher printed to its stderr, which systemd sends to the journal.
    def log(self) -> str:
        return self.log_path.read_text(errors="replace")

    def exit_code(self, timeout=10) -> int:
        assert self.proc is not None
        try:
            return self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"still running after {timeout} s\n{self.log()}"
            ) from None

    def host_pid(self) -> int:
        """The launcher's PID as the host sees it (unshare --fork makes it a child of the process we started)."""
        assert self.proc is not None
        if not self.pid_namespace:
            return self.proc.pid
        pid = self.proc.pid
        return int(
            wait_for(
                lambda: Path(f"/proc/{pid}/task/{pid}/children").read_text().split(),
                "the launcher process",
            )[0]
        )

    def restart(self) -> None:
        """What socket activation does after an idle exit: a new launcher on the same, still listening, socket."""
        assert self.proc is not None and self.proc.poll() is not None, "still running"
        self.start()

    def close(self) -> None:
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.proc.wait()
        self.listener.close()
        for leftover in self.sock_dir.iterdir():
            leftover.unlink()
        self.sock_dir.rmdir()

    # Requests
    def call(self, method, target, body=b"", headers=(), **kwargs) -> Reply:
        return exchange(
            self.sock_path, request(method, target, body, headers), **kwargs
        )

    def get(self, target) -> Reply:
        return self.call("GET", target)

    def post_json(self, target, payload, headers=()) -> Reply:
        return self.call(
            "POST",
            target,
            json.dumps(payload).encode(),
            ("Content-Type: application/json", *headers),
        )

    def status(self) -> dict:
        reply = self.get("/launcher/status")
        assert reply.status == 200, reply.raw
        return reply.json()

    def submit(self, model="zimage", prompt="a red fox", **fields) -> Reply:
        payload = {
            "prompt": prompt,
            "width": 64,
            "height": 64,
            "seed": 7,
            "sample_params": {"sample_steps": 4},
            "preview": "proj",
            "output_format": "webp",
            **fields,
        }
        return self.post_json(f"/e/{model}/sdcpp/v1/img_gen", payload)

    def poll(self, model, job_id) -> Reply:
        return self.get(f"/e/{model}/sdcpp/v1/jobs/{job_id}")

    def cancel(self, model, job_id) -> Reply:
        return self.call("POST", f"/e/{model}/sdcpp/v1/jobs/{job_id}/cancel")

    def kill(self) -> Reply:
        return self.call("POST", "/launcher/kill")

    def finish(self, model, job_id, timeout=10) -> Reply:
        """Poll until the job is over; a 410 also ends it."""

        def over():
            reply = self.poll(model, job_id)
            done = reply.status != 200 or reply.json()["status"] in (
                "completed",
                "failed",
                "cancelled",
            )
            return reply if done else None

        return wait_for(over, f"job {job_id} to end", timeout=timeout, rig=self)

    # What the stand-in recorded
    def records(self, name) -> list[dict]:
        path = self.engine_dir / name
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def starts(self) -> list[dict]:
        return self.records("starts.log")

    def engine_requests(self) -> list[dict]:
        return self.records("requests.log")

    def engine_pid(self) -> int:
        return int((self.engine_dir / "pid").read_text())

    def engines(self) -> list[int]:
        return stand_in_processes(str(self.engine_dir))


@pytest.fixture
def launch(tmp_path):
    rigs: list[Rig] = []

    def make(**options) -> Rig:
        rig = Rig(tmp_path / f"rig{len(rigs)}", **options)
        rigs.append(rig)
        return rig

    yield make
    for rig in rigs:
        rig.close()


@pytest.fixture(scope="module")
def quiet_rig(tmp_path_factory):
    """One launcher that all the table tests share. None of them may start an engine."""
    rig = Rig(tmp_path_factory.mktemp("quiet"), env={"AIHUB_IMAGES_CLIENT_S": "1"})
    yield rig
    try:
        assert rig.proc.poll() is None, rig.log()
        assert rig.status()["state"] == "idle"
        assert rig.starts() == [] and rig.engine_requests() == []
    finally:
        rig.close()


def test_the_documented_defaults_are_the_ones_in_the_code():
    # The ADR and the plan quote these numbers. Only the tests that shorten a timing would notice a change to
    # a default if nothing pinned it, and the unit sets none.
    spec = importlib.util.spec_from_file_location("images_launcher", LAUNCHER)
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    assert launcher.TIMES == {
        "IDLE": 300.0,
        "BACKOFF": 30.0,
        "START": 90.0,
        "GRACE": 5.0,
        "CLIENT": 30.0,
        "ENGINE": 60.0,
    }
    assert launcher.HEAD_MAX == HEAD_MAX == 16 * 1024
    assert launcher.BODY_MAX == BODY_MAX == 32 * 1024 * 1024
    assert launcher.REPLY_MAX == 16 * 1024 * 1024
    assert launcher.MAX_CONNECTIONS == 16
    assert launcher.RING == 20
    assert launcher.LINE_MAX == 1000
    assert launcher.LOG_LINES_MAX == 200


# --- start-up checks: the launcher fails closed with status 78 (EX_CONFIG) ---------------------------


class TestStartChecks:
    def test_refuses_to_run_outside_a_private_network_namespace(self, launch):
        rig = launch(namespaces=False)
        assert rig.exit_code() == 78
        assert "network" in rig.log()
        assert rig.engines() == []

    @pytest.mark.parametrize(
        ("listen_pid", "listen_fds"),
        [
            pytest.param("1234567", "1", id="pid-is-not-ours"),
            pytest.param(None, "1", id="no-pid"),
            pytest.param("self", None, id="no-fds"),
            pytest.param("self", "0", id="zero-fds"),
            pytest.param("self", "2", id="two-fds"),
            pytest.param("self", "one", id="fds-not-a-number"),
        ],
    )
    def test_needs_exactly_one_socket_for_itself(self, launch, listen_pid, listen_fds):
        rig = launch(listen_pid=listen_pid, listen_fds=listen_fds)
        assert rig.exit_code() == 78
        assert "LISTEN_" in rig.log()

    @pytest.mark.parametrize("fd3", ["file", "connected", "tcp"])
    def test_fd_3_must_be_a_listening_unix_socket(self, launch, fd3):
        rig = launch(fd3=fd3)
        assert rig.exit_code() == 78
        assert "fd 3" in rig.log()

    @pytest.mark.parametrize(
        "flag", ["-v", "--verbose", "--log-level", "--log-level=debug"]
    )
    @pytest.mark.parametrize("where", ["common", "model"])
    def test_refuses_verbose_logging_flags(self, launch, tmp_path, flag, where):
        # the shipped file is itself valid
        config = json.loads(SHIPPED_MODELS.read_text())
        config["engine"] = str(STAND_IN)
        if where == "common":
            config["common_args"].append(flag)
        else:
            config["models"]["zimage"].append(flag)
        path = tmp_path / "bad-models.json"
        path.write_text(json.dumps(config))
        rig = launch(models_file=path)
        assert rig.exit_code() == 78
        assert "models.json" in rig.log() and flag in rig.log()

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda c: c.pop("engine"), id="no-engine"),
            pytest.param(lambda c: c.update(engine=["a"]), id="engine-not-a-string"),
            pytest.param(lambda c: c.update(common_args="--x"), id="args-not-a-list"),
            pytest.param(lambda c: c.update(common_args=["--x", 1]), id="arg-not-text"),
            pytest.param(lambda c: c.update(models={}), id="no-models"),
            pytest.param(lambda c: c["models"].update({"Z Image": []}), id="bad-id"),
            pytest.param(lambda c: c["models"].update({"a" * 33: []}), id="long-id"),
            pytest.param(lambda c: c["models"].update(x=["a\x00b"]), id="nul-in-arg"),
            pytest.param(
                lambda c: c.update(common_args=["--listen-port", "18234"]),
                id="no-listen-ip",
            ),
            pytest.param(
                lambda c: c.update(common_args=["--listen-ip", "127.0.0.1"]),
                id="no-listen-port",
            ),
            pytest.param(
                lambda c: c.update(
                    common_args=["--listen-ip", "0.0.0.0", "--listen-port", "18234"]
                ),
                id="listens-on-every-address",
            ),
            pytest.param(
                lambda c: c.update(
                    common_args=["--listen-ip", "127.0.0.1", "--listen-port", "http"]
                ),
                id="port-not-a-number",
            ),
            pytest.param(
                lambda c: c["models"]["zimage"].extend(["--listen-port", "80"]),
                id="model-moves-the-port",
            ),
        ],
    )
    def test_refuses_a_bad_models_file(self, launch, tmp_path, mutate):
        config = json.loads(SHIPPED_MODELS.read_text())
        mutate(config)
        path = tmp_path / "bad-models.json"
        path.write_text(json.dumps(config))
        rig = launch(models_file=path)
        assert rig.exit_code() == 78
        assert "models.json" in rig.log()

    @pytest.mark.parametrize(
        ("key", "doubled"),
        [
            pytest.param(
                '"engine": ', '"engine": "/bin/false", "engine": ', id="engine"
            ),
            pytest.param(
                '"zimage": [', '"zimage": ["--other"], "zimage": [', id="model"
            ),
        ],
    )
    def test_refuses_a_models_file_that_says_a_key_twice(
        self, launch, tmp_path, key, doubled
    ):
        # json.loads keeps the last of two equal keys without a word, so the first would silently not count.
        config = json.loads(SHIPPED_MODELS.read_text())
        config["engine"] = str(STAND_IN)
        text = json.dumps(config)
        assert key in text
        path = tmp_path / "bad-models.json"
        path.write_text(text.replace(key, doubled, 1))
        rig = launch(models_file=path)
        assert rig.exit_code(timeout=3) == 78
        assert "models.json" in rig.log() and "twice" in rig.log()

    @pytest.mark.parametrize("text", ["{", "[]", '"x"', ""])
    def test_refuses_a_models_file_that_is_not_an_object(self, launch, tmp_path, text):
        path = tmp_path / "bad-models.json"
        path.write_text(text)
        rig = launch(models_file=path)
        assert rig.exit_code() == 78

    def test_refuses_a_missing_models_file(self, launch, tmp_path):
        rig = launch(models_file=tmp_path / "absent.json")
        assert rig.exit_code() == 78

    @pytest.mark.parametrize("value", ["0", "-1", "soon", "nan", "inf", ""])
    def test_refuses_a_bad_timing_override(self, launch, value):
        rig = launch(env={"AIHUB_IMAGES_IDLE_S": value})
        assert rig.exit_code() == 78
        assert "AIHUB_IMAGES_IDLE_S" in rig.log()

    def test_the_shipped_models_file_is_accepted(self, launch):
        # cwd is a scratch directory, so its relative engine path (../bin/sd-server) does not exist
        rig = launch(models_file=SHIPPED_MODELS)
        assert rig.status() == {"engine": None, "state": "idle", "last_error": []}
        reply = rig.submit("zimage")
        assert reply.status == 503
        assert reply.json()["code"] == "engine_failed_to_start"
        assert any("sd-server" in line for line in reply.json()["last_error"])
        assert rig.proc.poll() is None


# --- routes, start and forwarding ---------------------------------------------------------------------


class TestStatus:
    def test_status_does_not_start_an_engine(self, launch):
        rig = launch()
        for _ in range(3):
            reply = rig.get("/launcher/status")
            assert reply.status == 200
            assert reply.json() == {"engine": None, "state": "idle", "last_error": []}
        assert reply.headers["content-type"] == "application/json"
        assert reply.headers["connection"] == "close"
        assert int(reply.headers["content-length"]) == len(reply.body)
        assert rig.starts() == []

    def test_pid_1_in_a_pid_namespace_gets_its_socket(self, launch):
        # PrivatePIDs=yes: systemd sets LISTEN_PID to 1, which is also getpid() in the unit
        rig = launch(pid_namespace=True)
        assert rig.status()["state"] == "idle"
        assert rig.submit().status == 202


class TestStartAndForward:
    def test_the_first_img_gen_starts_the_engine_and_is_forwarded(self, launch):
        rig = launch()
        reply = rig.submit("zimage")
        assert reply.status == 202
        job = reply.json()
        assert job["id"].startswith("job_") and job["status"] == "queued"
        [start] = rig.starts()
        assert start["argv"] == [
            "--listen-ip",
            "127.0.0.1",
            "--listen-port",
            "18234",
            "--offload-to-cpu",
            "--max-vram",
            "3",
            "--standin-dir",
            str(rig.engine_dir),
            "--diffusion-model",
            "z-image-turbo/z_image_turbo-Q5_0.gguf",
        ]
        # the activated socket and its variables stay with the launcher
        assert start["fd3_open"] is False and start["listen_vars"] == {}
        assert rig.status() == {"engine": "zimage", "state": "ready", "last_error": []}

    def test_the_engine_inherits_a_short_list_of_variables_and_nothing_else(
        self, launch
    ):
        # Under systemd the launcher's environment is the user manager's: session bus, display, agent sockets.
        wanted = {
            "HOME": "/nonexistent-home",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CUDA_CACHE_DISABLE": "1",
            "CUDA_VISIBLE_DEVICES": "0",
            "GGML_CUDA_NO_PINNED": "1",
            "SD_MMAP_FLAGS": "1",
        }
        unwanted = {
            "DBUS_SESSION_BUS_ADDRESS": "unix:path=/nonexistent/bus",
            "DISPLAY": ":99",
            "XDG_RUNTIME_DIR": "/nonexistent/run",
            "SSH_AUTH_SOCK": "/nonexistent/agent",
            "SESSION_MANAGER": "local/nonexistent",
            "LD_PRELOAD": "/nonexistent/preload.so",
            "LD_LIBRARY_PATH": "/nonexistent/lib",
            "PYTHONPATH": "/nonexistent/python",
            "AIHUB_IMAGES_GRACE_S": "3",
            # names that only start like a listed one
            "LANGUAGE": "en",
            "HOMEBREW_PREFIX": "/nonexistent/brew",
            "CUDAFOO": "1",
            "PATHEXT": ".exe",
        }
        rig = launch(env={**wanted, **unwanted})
        assert rig.submit("zimage").status == 202
        [start] = rig.starts()
        seen = start["env"]
        assert {name: seen.get(name) for name in wanted} == wanted
        assert seen["PATH"] == os.environ["PATH"]
        assert set(unwanted) & set(seen) == set()
        assert not [name for name in seen if name.startswith("LISTEN_")]

    def test_a_job_runs_to_the_end_through_the_launcher(self, launch):
        rig = launch()
        job_id = rig.submit("zimage", width=1024, height=576).json()["id"]
        steps = []
        reply = wait_for(
            lambda: _collect(rig, "zimage", job_id, steps), "the job", rig=rig
        )
        job = reply.json()
        assert job["status"] == "completed" and job["error"] is None
        assert steps == sorted(steps) and steps[-1] == 4
        assert job["preview"]["total_steps"] == 4
        [image] = job["result"]["images"]
        webp = base64.b64decode(image["b64_json"], validate=True)
        assert webp[:4] == b"RIFF" and webp[8:16] == b"WEBPVP8 "
        width = int.from_bytes(webp[26:28], "little") & 0x3FFF
        height = int.from_bytes(webp[28:30], "little") & 0x3FFF
        assert (width, height) == (1024, 576)

    def test_the_warm_engine_serves_the_next_request(self, launch):
        rig = launch()
        rig.submit("zimage")
        pid = rig.engine_pid()
        rig.submit("zimage")
        assert len(rig.starts()) == 1 and rig.engine_pid() == pid

    def test_a_queued_job_can_be_cancelled_and_a_running_one_cannot(self, launch):
        rig = launch()
        running = rig.submit("zimage", prompt="slowimage").json()["id"]
        queued = rig.submit("zimage", prompt="another").json()["id"]
        wait_for(
            lambda: rig.poll("zimage", running).json()["status"] == "generating",
            "the first job to run",
            rig=rig,
        )
        assert rig.poll("zimage", queued).json()["queue_position"] == 1
        assert rig.cancel("zimage", queued).status == 200
        assert rig.cancel("zimage", running).status == 409
        assert rig.poll("zimage", queued).json()["status"] == "cancelled"

    def test_the_engine_gets_a_clean_re_serialised_request(self, launch):
        rig = launch()
        body = b'{"prompt":"a red fox","width":64,"height":64}'
        reply = rig.call(
            "POST",
            "/e/zimage/sdcpp/v1/img_gen",
            body,
            (
                "Content-Type: application/json; charset=utf-8",
                "Cookie: session=abc",
                "Authorization: Bearer abc",
                "X-Forwarded-For: 10.0.0.1",
                "Origin: http://evil.example",
                "Accept-Encoding: gzip",
                "Expect: 100-continue",
                "Upgrade: websocket",
                "Keep-Alive: 300",
            ),
        )
        assert reply.status == 202
        job_id = reply.json()["id"]
        rig.poll("zimage", job_id)
        rig.cancel("zimage", job_id)
        submitted, polled, cancelled = rig.engine_requests()
        assert submitted["head"] == (
            "POST /sdcpp/v1/img_gen HTTP/1.1\r\n"
            f"{CANON_HOST}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n\r\n"
        )
        assert submitted["body_sha256"] == hashlib.sha256(body).hexdigest()
        assert polled["head"] == (
            f"GET /sdcpp/v1/jobs/{job_id} HTTP/1.1\r\n{CANON_HOST}\r\nConnection: close\r\n\r\n"
        )
        assert cancelled["head"] == (
            f"POST /sdcpp/v1/jobs/{job_id}/cancel HTTP/1.1\r\n{CANON_HOST}\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n"
        )

    def test_a_body_on_a_route_that_takes_none_is_dropped(self, launch):
        rig = launch()
        job_id = rig.submit("zimage", prompt="slowimage").json()["id"]
        wait_for(
            lambda: rig.poll("zimage", job_id).json()["status"] == "generating",
            "the job to run",
            rig=rig,
        )
        reply = rig.call("POST", f"/e/zimage/sdcpp/v1/jobs/{job_id}/cancel", b"x" * 100)
        assert reply.status == 409
        assert rig.engine_requests()[-1]["body_len"] == 0
        assert rig.call("POST", "/launcher/kill", b"x" * 100).json() == {"killed": True}

    def test_img_gen_needs_a_length_and_a_json_type(self, launch):
        rig = launch()
        target = "/e/zimage/sdcpp/v1/img_gen"
        no_length = f"POST {target} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\n\r\n"
        assert exchange(rig.sock_path, no_length.encode()).status == 411
        wrong_type = rig.call("POST", target, b"{}", ("Content-Type: text/plain",))
        assert wrong_type.status == 415
        no_type = rig.call("POST", target, b"{}")
        assert no_type.status == 415
        assert rig.starts() == [] and rig.engine_requests() == []


def _collect(rig, model, job_id, steps):
    """Poll once, record the preview step, and return the reply when the job has completed."""
    reply = rig.poll(model, job_id)
    job = reply.json()
    if job.get("preview"):
        steps.append(job["preview"]["step"])
    return reply if job["status"] in ("completed", "failed") else None


class TestModelSwitch:
    def test_a_second_model_replaces_the_first_engine(self, launch):
        rig = launch()
        first = rig.submit("zimage").json()["id"]
        [first_pid] = rig.engines()
        assert rig.submit("klein4b").status == 202
        [second_pid] = rig.engines()  # never two engines at once
        assert second_pid != first_pid and not alive(first_pid)
        assert rig.status()["engine"] == "klein4b"
        assert len(rig.starts()) == 2
        # a poll for the job of the engine that was replaced is gone, and starts nothing
        reply = rig.poll("zimage", first)
        assert reply.status == 410 and reply.json()["code"] == "engine_gone"
        assert len(rig.starts()) == 2

    def test_an_engine_that_ignores_sigterm_is_killed_after_the_grace_period(
        self, launch
    ):
        rig = launch(env={"AIHUB_IMAGES_GRACE_S": "0.6"})
        assert rig.submit("stubborn").status == 202
        [stubborn] = rig.engines()
        began = time.monotonic()
        assert rig.submit("zimage").status == 202
        assert time.monotonic() - began >= 0.6
        assert not alive(stubborn) and len(rig.engines()) == 1


class TestEngineGone:
    def test_polls_without_an_engine_get_410_and_start_nothing(self, launch):
        rig = launch()
        for reply in (
            rig.poll("zimage", "job_abc"),
            rig.cancel("zimage", "job_abc"),
        ):
            assert reply.status == 410
            assert reply.json() == {"code": "engine_gone", "last_error": []}
        assert rig.starts() == [] and rig.status()["state"] == "idle"

    def test_the_engine_crashing_during_a_job_is_a_410_and_the_next_job_restarts(
        self, launch
    ):
        rig = launch()
        job_id = rig.submit("zimage", prompt="crashimage").json()["id"]
        gone = rig.finish("zimage", job_id)
        assert gone.status == 410 and gone.json()["code"] == "engine_gone"
        errors = gone.json()["last_error"]
        assert "[E] VAE decode failed --- fake_sd_server.py:1" in errors
        assert any("SIGABRT" in line for line in errors)
        status = rig.status()
        assert status["state"] == "idle" and status["engine"] is None
        assert status["last_error"] == errors  # kept for the polls that follow
        assert rig.submit("zimage").status == 202
        assert len(rig.starts()) == 2 and rig.status()["last_error"] == []

    def test_polls_for_an_unknown_model_are_404(self, launch):
        rig = launch()
        for reply in (
            rig.submit("nope"),
            rig.poll("nope", "job_abc"),
            rig.cancel("nope", "job_abc"),
        ):
            assert reply.status == 404 and reply.json() == {"code": "unknown_model"}
        assert rig.starts() == []


class TestFailSoft:
    def test_a_failing_start_gets_503_and_one_attempt_serves_five_requests(
        self, launch
    ):
        rig = launch()
        replies = [rig.submit("exit3") for _ in range(5)]
        assert {r.status for r in replies} == {503}
        body = replies[0].json()
        assert body["code"] == "engine_failed_to_start"
        assert (
            "[E] cannot inspect model source 'z-image-turbo/z_image_turbo-Q5_0.gguf': No such file or directory --- fake_sd_server.py:1"
            in body["last_error"]
        )
        assert any("status 3" in line for line in body["last_error"])
        assert all(r.json() == body for r in replies)
        assert len(rig.starts()) == 1
        assert rig.proc.poll() is None
        assert rig.status() == {
            "engine": None,
            "state": "failed",
            "last_error": body["last_error"],
        }
        # the warning went to stdout, the error to stderr; both are in the ring
        assert any(line.startswith("[W] backend CUDA0") for line in body["last_error"])

    def test_the_backoff_also_holds_for_other_models_and_then_ends(self, launch):
        rig = launch(env={"AIHUB_IMAGES_BACKOFF_S": "1.5"})
        assert rig.submit("exit3").status == 503
        other = rig.submit("zimage")
        assert other.status == 503 and len(rig.starts()) == 1
        # a poll never starts an engine
        assert rig.get("/e/zimage/sdcpp/v1/jobs/job_x").status == 410
        time.sleep(1.6)
        assert rig.submit("zimage").status == 202
        assert len(rig.starts()) == 2
        assert rig.status()["state"] == "ready"

    def test_an_engine_that_never_listens_is_killed_after_the_start_timeout(
        self, launch
    ):
        rig = launch(env={"AIHUB_IMAGES_START_S": "1"})
        began = time.monotonic()
        reply = rig.submit("hangs")
        assert 1 <= time.monotonic() - began < 6
        assert reply.status == 503
        assert any("did not listen within 1 s" in x for x in reply.json()["last_error"])
        wait_for(lambda: rig.engines() == [], "the hung engine to be killed", rig=rig)
        assert rig.status()["state"] == "failed"

    def test_an_engine_that_aborts_before_listening_is_named_by_its_signal(
        self, launch
    ):
        rig = launch()
        reply = rig.submit("crashes")
        assert reply.status == 503
        lines = reply.json()["last_error"]
        assert "[E] ggml_cuda_init failed --- fake_sd_server.py:1" in lines
        assert any("SIGABRT" in line for line in lines)

    def test_an_engine_that_cannot_be_executed_is_a_503(self, launch):
        rig = launch(engine="/nonexistent/sd-server")
        reply = rig.submit("zimage")
        assert reply.status == 503
        assert any("/nonexistent/sd-server" in x for x in reply.json()["last_error"])
        assert rig.proc.poll() is None and rig.status()["state"] == "failed"

    def test_a_flood_during_start_is_bounded(self, launch):
        rig = launch()
        reply = rig.submit("noisy")
        assert reply.status == 202
        errors = rig.status()["last_error"]
        assert len(errors) == 20
        assert errors[-1] == "[E] last error line --- fake_sd_server.py:1"
        # a 5000-character line is cut to 1000 characters
        assert errors[-2] == "[E] " + "z" * 996
        assert all(len(line) <= 1000 for line in errors)
        log = rig.log()
        assert len(log) < 150_000 and log.count("not logged") == 1
        # the first 200 lines of a start go to the journal, and no more
        assert log.count("launcher: engine noisy: ") == 200


class TestKill:
    def test_kill_ends_the_engine_before_it_answers(self, launch):
        # Once is not enough to tell "gone before the answer" from "gone a moment after it": a SIGKILLed
        # process dies within microseconds. Eight rounds make a launcher that answers early fail.
        rig = launch()
        for _ in range(8):
            job_id = rig.submit("zimage", prompt="slowimage").json()["id"]
            [pid] = rig.engines()
            assert rig.kill().json() == {"killed": True}
            assert not alive(pid)
            assert rig.poll("zimage", job_id).status == 410
            assert rig.status()["state"] == "idle"
        assert rig.kill().json() == {"killed": False}

    def test_kill_without_an_engine(self, launch):
        assert launch().kill().json() == {"killed": False}

    def test_kill_while_the_engine_loads_ends_the_start_without_a_backoff(self, launch):
        rig = launch()
        answers = []
        thread = threading.Thread(target=lambda: answers.append(rig.submit("slowload")))
        thread.start()
        wait_for(lambda: rig.status()["state"] == "starting", "the start", rig=rig)
        # the stand-in writes its start record first, before it "loads"
        wait_for(rig.starts, "the engine to begin loading", rig=rig)
        assert rig.kill().json() == {"killed": True}
        thread.join(10)
        assert answers[0].status == 503
        assert rig.status()["state"] == "idle"
        # not held back by a backoff
        assert rig.submit("zimage").status == 202
        assert len(rig.starts()) == 2


class TestWantedRequests:
    """An img_gen is dropped when a kill or a hang-up overtakes it: nobody would poll its job, and the GPU would
    be busy with it after aihub has let go of the GPU lock."""

    def test_a_client_that_hangs_up_before_the_engine_starts_costs_no_start(
        self, launch
    ):
        rig = launch()
        open_request(rig, "slowload").close()
        wait_for(lambda: "hung up" in rig.log(), "the launcher to notice", rig=rig)
        assert rig.starts() == [] and rig.engine_requests() == []
        assert rig.status()["state"] == "idle"

    def test_a_client_that_hangs_up_while_the_engine_loads_gets_no_job(self, launch):
        rig = launch()
        conn = open_request(rig, "slowload")
        wait_for(rig.starts, "the engine to begin loading", rig=rig)
        conn.close()
        wait_for(lambda: "hung up" in rig.log(), "the launcher to notice", rig=rig)
        # the engine stays up, with nothing asked of it
        assert rig.status() == {
            "engine": "slowload",
            "state": "ready",
            "last_error": [],
        }
        assert rig.engine_requests() == []

    def test_a_client_that_only_shuts_down_its_sending_side_is_still_answered(
        self, launch
    ):
        rig = launch()
        conn = open_request(rig, "zimage")
        conn.shutdown(socket.SHUT_WR)
        assert read_reply(conn).status == 202
        assert "hung up" not in rig.log()

    def test_a_kill_during_a_model_switch_stops_the_switch(self, launch):
        rig = launch(env={"AIHUB_IMAGES_GRACE_S": "3"})
        assert rig.submit("stubborn").status == 202
        conn = open_request(rig, "zimage")
        # the launcher has sent SIGTERM to the engine that ignores it, and waits out the grace period
        wait_for(
            lambda: rig.records("signals.log"), "SIGTERM to the old engine", rig=rig
        )
        assert rig.kill().json() == {"killed": True}
        reply = read_reply(conn)
        assert reply.status == 503 and reply.json() == {"code": "killed"}
        assert len(rig.starts()) == 1 and rig.engines() == []
        assert rig.status()["state"] == "idle"

    def test_a_kill_that_overtakes_a_request_still_arriving_drops_it(self, launch):
        rig = launch()
        body = json.dumps({"prompt": "a red fox", "width": 64, "height": 64}).encode()
        full = request(
            "POST",
            "/e/zimage/sdcpp/v1/img_gen",
            body,
            ("Content-Type: application/json",),
        )
        cut = len(full) - len(body) + 5  # the head and 5 bytes of the body
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(15)
            conn.connect(rig.sock_path)
            conn.sendall(full[:cut])
            assert rig.kill().json() == {"killed": False}
            conn.sendall(full[cut:])
            reply = parse_reply(conn.recv(1 << 16))
        assert reply.status == 503 and reply.json() == {"code": "killed"}
        assert rig.starts() == []

    def test_a_kill_reaches_a_request_that_waits_for_another_start(self, launch):
        rig = launch()
        first = open_request(rig, "slowload")
        wait_for(rig.starts, "the engine to begin loading", rig=rig)
        second = open_request(rig, "zimage")  # waits for the first start to be over
        assert rig.kill().json() == {"killed": True}
        assert read_reply(first).json()["code"] == "engine_failed_to_start"
        reply = read_reply(second)
        assert reply.status == 503 and reply.json() == {"code": "killed"}
        assert len(rig.starts()) == 1
        # a request made after the kill is not touched by it
        assert rig.submit("zimage").status == 202


class TestConcurrency:
    def test_status_answers_while_an_engine_loads_and_two_requests_share_the_start(
        self, launch
    ):
        rig = launch()
        answers = []
        threads = [
            threading.Thread(target=lambda: answers.append(rig.submit("slowload")))
            for _ in range(2)
        ]
        for thread in threads:
            thread.start()
        status = wait_for(
            lambda: (s := rig.status())["state"] == "starting" and s,
            "the start",
            rig=rig,
        )
        assert status["engine"] == "slowload"
        for thread in threads:
            thread.join(15)
        assert [a.status for a in answers] == [202, 202]
        assert len(rig.starts()) == 1

    def test_connections_beyond_the_limit_are_turned_away(self, launch):
        rig = launch(env={"AIHUB_IMAGES_CLIENT_S": "5"})
        idle = []
        try:
            for _ in range(16):
                conn = socket.socket(socket.AF_UNIX)
                conn.connect(rig.sock_path)
                idle.append(conn)
            # the launcher accepts the idle connections asynchronously; once it holds all 16 it turns the next away
            reply = wait_for(
                lambda: (r := rig.get("/launcher/status")).status == 503 and r,
                "the connection limit",
                rig=rig,
            )
            assert reply.json() == {"code": "busy"}
        finally:
            for conn in idle:
                conn.close()
        wait_for(
            lambda: rig.get("/launcher/status").status == 200, "a free slot", rig=rig
        )

    def test_running_out_of_descriptors_pauses_accepting_instead_of_spinning(
        self, launch
    ):
        # With no descriptor left, accept() fails and the connection stays queued, so the listening socket
        # stays readable: a reader that only logs and returns is called again at once, without end.
        rig = launch(nofile=20, env={"AIHUB_IMAGES_CLIENT_S": "5"})
        waiting = []
        try:
            for _ in range(40):
                conn = socket.socket(socket.AF_UNIX)
                conn.connect(rig.sock_path)
                waiting.append(conn)
            wait_for(
                lambda: "accept failed" in rig.log(),
                "the launcher to run out of descriptors",
                rig=rig,
            )
            logged, spent = rig.log_path.stat().st_size, cpu_ticks(rig.proc.pid)
            time.sleep(1.0)
            assert rig.log_path.stat().st_size - logged < 4000
            assert (
                cpu_ticks(rig.proc.pid) - spent < 30
            )  # a spin costs about 100 a second
        finally:
            for conn in waiting:
                conn.close()
        # it picks the queue up again once descriptors are free
        wait_for(
            lambda: rig.get("/launcher/status").status == 200,
            "the launcher to serve again",
            rig=rig,
        )
        assert rig.proc.poll() is None


# --- idle exit, signals and the engine's lifetime ---------------------------------------------------


class TestLifetime:
    def test_idle_exit_stops_the_engine_and_the_launcher_and_the_next_request_restarts(
        self, launch
    ):
        rig = launch(env={"AIHUB_IMAGES_IDLE_S": "1"})
        assert rig.submit("zimage").status == 202
        assert rig.engines() != []
        assert rig.exit_code(timeout=8) == 0
        assert rig.engines() == []
        assert "idle" in rig.log()
        # socket activation: the socket stayed open, so the next client starts a new launcher
        rig.restart()
        assert rig.status()["state"] == "idle"
        assert rig.submit("zimage").status == 202
        assert len(rig.starts()) == 2

    def test_every_connection_restarts_the_idle_clock(self, launch):
        # Six rounds of 0.5 s outlast the 2 s timeout, and each stays well inside it.
        rig = launch(env={"AIHUB_IMAGES_IDLE_S": "2"})
        for _ in range(6):
            time.sleep(0.5)
            assert rig.proc.poll() is None
            rig.status()
        assert rig.exit_code(timeout=10) == 0

    def test_a_connection_in_flight_keeps_the_launcher_up(self, launch):
        rig = launch(env={"AIHUB_IMAGES_IDLE_S": "0.6", "AIHUB_IMAGES_CLIENT_S": "5"})
        conn = socket.socket(socket.AF_UNIX)
        conn.connect(rig.sock_path)
        time.sleep(1.5)
        assert rig.proc.poll() is None
        conn.close()
        assert rig.exit_code(timeout=8) == 0

    def test_sigterm_stops_the_engine_and_exits_zero_even_as_pid_1(self, launch):
        rig = launch(pid_namespace=True)
        assert rig.submit("zimage").status == 202
        os.kill(rig.host_pid(), signal.SIGTERM)
        assert rig.exit_code(timeout=8) == 0
        assert rig.engines() == []

    def test_sigterm_during_an_engine_start_leaves_nothing_behind(self, launch):
        rig = launch(pid_namespace=True)
        thread = threading.Thread(target=lambda: rig.submit("slowload"))
        thread.start()
        wait_for(lambda: rig.status()["state"] == "starting", "the start", rig=rig)
        os.kill(rig.host_pid(), signal.SIGTERM)
        assert rig.exit_code(timeout=8) == 0
        thread.join(10)
        assert rig.engines() == []

    def test_the_engine_dies_with_a_launcher_that_is_killed_outright(self, launch):
        rig = launch()
        rig.submit("zimage")
        [pid] = rig.engines()
        os.kill(rig.proc.pid, signal.SIGKILL)
        wait_for(lambda: not alive(pid), "the engine to die with the launcher", rig=rig)

    def test_the_engine_dies_with_a_launcher_that_is_pid_1(self, launch):
        # PrivatePIDs=yes: the kernel ends the namespace when its PID 1 goes
        rig = launch(pid_namespace=True)
        rig.submit("zimage")
        assert rig.engines() != []
        os.kill(rig.host_pid(), signal.SIGKILL)
        wait_for(lambda: rig.engines() == [], "the engine to die with PID 1", rig=rig)


@pytest.mark.parametrize(
    "python", INTERPRETERS, ids=lambda p: Path(os.path.realpath(p)).name
)
def test_runs_on_both_interpreters(launch, python):
    rig = launch(python=python, env={"AIHUB_IMAGES_IDLE_S": "1.5"})
    assert rig.status()["state"] == "idle"
    job_id = rig.submit("zimage").json()["id"]
    assert rig.finish("zimage", job_id).json()["status"] == "completed"
    assert rig.exit_code(timeout=10) == 0


# --- what the launcher says about the engine -----------------------------------------------------------


class TestLogging:
    def test_prompts_stay_out_of_the_launcher_log_but_error_lines_get_in(self, launch):
        rig = launch()
        marker = "needle-5f3a-prompt"
        job_id = rig.submit("zimage", prompt=f"imagefail {marker}").json()["id"]
        ended = rig.finish("zimage", job_id).json()
        assert ended["status"] == "failed"
        wait_for(lambda: "[E] generate failed" in rig.log(), "the error line", rig=rig)
        log = rig.log()
        assert marker not in log
        assert "listening on" not in log and "[I]" not in log
        assert "[E] generate failed" in log
        assert "[W] model manager cannot make enough memory available on CUDA0" in log
        # aihub reads the same lines from the status route
        lines = rig.status()["last_error"]
        assert "[E] generate failed --- fake_sd_server.py:1" in lines
        # ggml's own error lines have no file and line at the end, and are kept all the same
        assert (
            "[E] ggml: ggml_backend_cuda_buffer_type_alloc_buffer: allocating 1021.54 MiB on device 0: cudaMalloc failed: CUDA-capable device(s) is/are busy or unavailable"
            in lines
        )
        assert rig.status()["state"] == "ready"

    def read_up_to_here(self, rig, token):
        """Wait until the launcher has read what the engine printed so far: the pipe keeps the order."""
        job_id = rig.submit("zimage", prompt=f"sentinel-{token}").json()["id"]
        rig.finish("zimage", job_id)
        wait_for(lambda: f"[E] sentinel {token}" in rig.log(), "the log", rig=rig)

    def test_a_line_that_looks_like_an_error_inside_a_prompt_message_stays_out(
        self, launch
    ):
        # The stand-in logs the prompt at [I], in one message, and a newline in the prompt makes two lines.
        # (sd-server logs prompts only at verbose level, which the launcher refuses, and escapes newlines.)
        rig = launch()
        marker = "needle-7c1e-prompt"
        rig.finish(
            "zimage", rig.submit("zimage", prompt=f"a fox\n[E] {marker}").json()["id"]
        )
        self.read_up_to_here(rig, "one")
        assert marker not in rig.log()
        assert not [line for line in rig.status()["last_error"] if marker in line]

    def test_the_second_half_of_a_long_line_is_not_a_line_of_its_own(self, launch):
        rig = launch()
        rig.finish(
            "zimage", rig.submit("zimage", prompt="longimage").json()["id"], timeout=15
        )
        self.read_up_to_here(rig, "two")
        assert "LEAKMARK" not in rig.log()
        assert not [x for x in rig.status()["last_error"] if "LEAKMARK" in x]

    def test_each_shape_of_engine_line_is_judged_as_the_lab_logs_show_it(self, launch):
        rig = launch()
        rig.finish(
            "zimage", rig.submit("zimage", prompt="logshapes").json()["id"], timeout=15
        )
        self.read_up_to_here(rig, "one")
        assert rig.status()["last_error"] == [
            "[W] ggml: a warning from ggml",
            "[E] kept after a ggml line --- fake_sd_server.py:1",
            "[E] ggml: an error from ggml",
            # a message that never ends is dropped for 50 lines and not for longer
            *(f"[E] line {n} of a message that never ends" for n in range(50, 60)),
            "[E] kept after the long message --- fake_sd_server.py:1",
            "[E] sentinel one --- fake_sd_server.py:1",
        ]
        log = rig.log()
        assert "only looks like an error" not in log and "a stray line" not in log

    def test_a_flood_during_a_job_keeps_the_last_20_lines_and_a_short_log(self, launch):
        rig = launch()
        job_id = rig.submit("zimage", prompt="noisyimage").json()["id"]
        began = time.monotonic()
        assert rig.status()["state"] == "ready"
        assert time.monotonic() - began < 2  # the flood does not stall the launcher
        wait_for(
            lambda: (
                rig.status()["last_error"][-1:]
                == ["[E] last error line --- fake_sd_server.py:1"]
            ),
            "the flood to end",
            rig=rig,
        )
        errors = rig.status()["last_error"]
        assert len(errors) == 20 and all(len(x) <= 1000 for x in errors)
        assert errors[-2] == "[E] " + "z" * 996
        assert rig.finish("zimage", job_id).json()["status"] == "completed"
        assert len(rig.log()) < 150_000


# --- the engine's reply is untrusted ---------------------------------------------------------------------


class TestEngineReplies:
    @pytest.mark.parametrize(
        "fault",
        [
            "nolength",
            "chunked",
            "chunkedlength",
            "twolengths",
            "hugehead",
            "hugebody",
            "truncated",
            "badstatus",
            "garbage",
        ],
    )
    def test_a_reply_with_bad_framing_is_a_502(self, launch, fault):
        rig = launch()
        reply = rig.submit("zimage", prompt=f"fault-{fault}")
        assert reply.status == 502 and reply.json() == {"code": "bad_engine_reply"}
        assert len(reply.raw) < 1000  # nothing of the engine's reply is passed on
        # the engine itself is fine, and the next request is answered
        assert rig.status()["state"] == "ready"
        assert rig.submit("zimage").status == 202

    def test_a_reply_that_stalls_is_a_504(self, launch):
        rig = launch(env={"AIHUB_IMAGES_ENGINE_S": "1"})
        began = time.monotonic()
        reply = rig.submit("zimage", prompt="fault-stall")
        assert reply.status == 504 and reply.json() == {"code": "engine_timeout"}
        assert time.monotonic() - began < 5

    def test_the_content_type_of_a_good_reply_is_passed_on(self, launch):
        rig = launch()
        reply = rig.submit("zimage")
        assert reply.headers["content-type"] == "application/json"
        assert int(reply.headers["content-length"]) == len(reply.body)


# --- what a client may send ------------------------------------------------------------------------------

NOT_FOUND = [
    ("GET", "/"),
    ("GET", "/launcher"),
    ("GET", "/launcher/"),
    ("GET", "/launcher/status/"),
    ("GET", "/launcher/statu"),
    ("GET", "/launcher/status?x=1"),
    ("GET", "/launcher/../launcher/status"),
    ("GET", "//launcher/status"),
    ("GET", "/LAUNCHER/status"),
    ("POST", "/launcher/status"),
    ("GET", "/launcher/kill"),
    ("POST", "/launcher/kill/"),
    ("POST", "/launcher/kill?now=1"),
    ("DELETE", "/launcher/status"),
    ("PUT", "/launcher/kill"),
    ("HEAD", "/launcher/status"),
    ("OPTIONS", "/launcher/status"),
    ("PATCH", "/e/zimage/sdcpp/v1/img_gen"),
    ("GET", "/e/zimage/sdcpp/v1/img_gen"),
    ("POST", "/e/zimage/sdcpp/v1/jobs/job_1"),
    ("GET", "/e/zimage/sdcpp/v1/jobs/job_1/cancel"),
    ("POST", "/e/zimage/sdcpp/v1/img_gen/"),
    ("POST", "/e/zimage/sdcpp/v1/img_gen?x=1"),
    ("POST", "/e/zimage/sdcpp/v1/capabilities"),
    ("GET", "/e/zimage/sdcpp/v1/capabilities"),
    ("POST", "/e/zimage/sdcpp/v1/upscale"),
    ("POST", "/e/zimage/sdcpp/v1/vid_gen"),
    ("GET", "/e/zimage/sdcpp/v1/jobs"),
    ("GET", "/e/zimage/sdcpp/v1/jobs/"),
    ("GET", "/e/zimage/sdcpp/v1/jobs/job_1/"),
    ("POST", "/e/zimage/sdcpp/v1/jobs/job_1/cancel/"),
    ("GET", "/e/zimage/sdcpp/v1/jobs/.."),
    ("GET", "/e/zimage/sdcpp/v1/jobs/../../../v1/models"),
    ("GET", "/e/zimage/sdcpp/v1/jobs/%2e%2e"),
    ("GET", "/e/zimage/sdcpp/v1/jobs/job_1%0d%0aX-Evil:%201"),
    ("GET", "/e/zimage/sdcpp/v1/jobs/job.1"),
    ("GET", f"/e/zimage/sdcpp/v1/jobs/{'a' * 65}"),
    ("POST", f"/e/zimage/sdcpp/v1/jobs/{'a' * 65}/cancel"),
    ("POST", "/e/zimage/v1/images/generations"),
    ("POST", "/e/zimage/v1/images/edits"),
    ("POST", "/e/zimage/sdapi/v1/txt2img"),
    ("GET", "/e/zimage/sdapi/v1/progress"),
    ("GET", "/e/zimage/v1/models"),
    ("POST", "/v1/images/generations"),
    ("POST", "/sdapi/v1/txt2img"),
    ("POST", "/sdcpp/v1/img_gen"),
    ("GET", "/sdcpp/v1/jobs/job_1"),
    ("GET", "/sdcpp/v1/capabilities"),
    ("GET", "/e/ZIMAGE/sdcpp/v1/jobs/job_1"),
    ("GET", "/e/zi_mage/sdcpp/v1/jobs/job_1"),
    ("GET", f"/e/{'a' * 33}/sdcpp/v1/jobs/job_1"),
    ("GET", "/e//sdcpp/v1/jobs/job_1"),
    ("GET", "/e/zimage"),
    ("GET", "/e/zimage/"),
    ("GET", "/e/zimage/sdcpp"),
    ("GET", "/e/zimage/sdcpp/v1/jobs/job_1/cancel/extra"),
    ("GET", "/e/zimage/../launcher/status"),
]


class TestRouteAllowlist:
    @pytest.mark.parametrize(
        ("method", "target"), NOT_FOUND, ids=[f"{m} {t[:50]}" for m, t in NOT_FOUND]
    )
    def test_anything_else_is_404_and_never_reaches_an_engine(
        self, quiet_rig, method, target
    ):
        reply = quiet_rig.call(method, target)
        assert reply.status == 404 and reply.json() == {"code": "not_found"}


def pad_head(prefix: str, size: int) -> bytes:
    """A request of exactly `size` bytes up to and including the blank line."""
    fill = size - len(prefix) - len("X-Pad: \r\n\r\n")
    return f"{prefix}X-Pad: {'a' * fill}\r\n\r\n".encode()


MALFORMED = [
    pytest.param(b"GET /launcher/status HTTP/1.1\nHost: x\n\n", id="bare-lf-lines"),
    pytest.param(
        b"GET /launcher/status HTTP/1.1\r\nHost: x\r\nX: a\nb\r\n\r\n",
        id="bare-lf-in-value",
    ),
    pytest.param(
        b"GET /launcher/status HTTP/1.1\r\nHost: x\r\nX: a\rb\r\n\r\n",
        id="bare-cr-in-value",
    ),
    pytest.param(
        b"GET /launcher/status HTTP/1.1\r\nHost: x\r\nX: a\x00b\r\n\r\n",
        id="nul-in-value",
    ),
    pytest.param(
        b"GET /launcher/status HTTP/1.1\r\nHost: x\r\nX: caf\xc3\xa9\r\n\r\n",
        id="non-ascii",
    ),
    pytest.param(
        b"GET /launcher/status HTTP/1.1\r\nX: a\r\n b\r\n\r\n",
        id="obs-fold-continuation",
    ),
    pytest.param(
        b"GET /launcher/status HTTP/1.1\r\nHost: x\r\nnocolon\r\n\r\n",
        id="header-without-colon",
    ),
    pytest.param(
        b"GET /launcher/status HTTP/1.1\r\nHost : x\r\n\r\n", id="space-before-colon"
    ),
    pytest.param(b"GET  /launcher/status HTTP/1.1\r\n\r\n", id="two-spaces"),
    pytest.param(b"GET\t/launcher/status HTTP/1.1\r\n\r\n", id="tab-in-request-line"),
    pytest.param(b"get /launcher/status HTTP/1.1\r\n\r\n", id="lowercase-method"),
    pytest.param(b"GET /launcher/status\r\n\r\n", id="http-0.9"),
    pytest.param(b"GET /launcher/status HTTP/2.0\r\n\r\n", id="http-2"),
    pytest.param(b"GET /launcher/status HTTP/1.2\r\n\r\n", id="http-1.2"),
    pytest.param(
        b"GET http://engine/launcher/status HTTP/1.1\r\n\r\n", id="absolute-form"
    ),
    pytest.param(b"OPTIONS * HTTP/1.1\r\n\r\n", id="asterisk-form"),
    pytest.param(b"CONNECT engine:80 HTTP/1.1\r\n\r\n", id="connect"),
    pytest.param(
        b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03\r\n\r\n", id="tls-hello"
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        id="chunked",
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nContent-Length: 4\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n",
        id="content-length-and-chunked",
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nTransfer-Encoding: x\r\n chunked\r\n\r\n",
        id="folded-transfer-encoding",
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nContent-Length: 4\r\nContent-Length: 7\r\n\r\nabcdefg",
        id="two-different-lengths",
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nContent-Length: 4\r\nContent-Length: 4\r\n\r\nabcd",
        id="two-equal-lengths",
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nContent-Length: +4\r\n\r\nabcd",
        id="plus-length",
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nContent-Length: -4\r\n\r\n",
        id="negative-length",
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nContent-Length: 0x4\r\n\r\nabcd",
        id="hex-length",
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nContent-Length: 4, 4\r\n\r\nabcd",
        id="list-length",
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nContent-Length: 1e1\r\n\r\n",
        id="exponent-length",
    ),
    pytest.param(
        b"POST /launcher/kill HTTP/1.1\r\nContent-Length:\r\n\r\n", id="empty-length"
    ),
    pytest.param(
        "POST /launcher/kill HTTP/1.1\r\nContent-Length: ４\r\n\r\nabcd".encode(),
        id="fullwidth-digit-length",
    ),
]


class TestMalformedRequests:
    @pytest.mark.parametrize("data", MALFORMED)
    def test_a_malformed_request_is_a_400_and_does_nothing(self, quiet_rig, data):
        reply = exchange(quiet_rig.sock_path, data, half_close=True)
        assert reply.status == 400 and reply.json() == {"code": "bad_request"}

    def test_a_body_that_ends_early_is_a_400(self, quiet_rig):
        data = (
            b"POST /e/zimage/sdcpp/v1/img_gen HTTP/1.1\r\n"
            b"Content-Type: application/json\r\nContent-Length: 10\r\n\r\nabc"
        )
        assert exchange(quiet_rig.sock_path, data, half_close=True).status == 400

    def test_a_connection_that_sends_nothing_is_ignored(self, quiet_rig):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.connect(quiet_rig.sock_path)
        assert quiet_rig.status()["state"] == "idle"

    def test_a_slow_head_is_a_408(self, quiet_rig):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(8)
            conn.connect(quiet_rig.sock_path)
            conn.sendall(b"GET /launcher/status HTTP/1.1\r\nHost: x\r\n")  # never ends
            reply = parse_reply(conn.recv(4096))
        assert reply.status == 408 and reply.json() == {"code": "request_timeout"}

    def test_a_slow_body_is_a_408(self, quiet_rig):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(8)
            conn.connect(quiet_rig.sock_path)
            conn.sendall(
                b"POST /e/zimage/sdcpp/v1/img_gen HTTP/1.1\r\n"
                b"Content-Type: application/json\r\nContent-Length: 10\r\n\r\nabc"
            )
            assert parse_reply(conn.recv(4096)).status == 408

    def test_extra_bytes_after_the_request_are_ignored(self, quiet_rig):
        smuggled = request("POST", "/launcher/kill")
        reply = exchange(
            quiet_rig.sock_path, request("GET", "/launcher/status") + smuggled
        )
        assert reply.status == 200
        assert reply.raw.count(b"HTTP/1.1 ") == 1


class TestLimits:
    def test_a_head_of_16_kib_is_accepted_and_one_byte_more_is_not(self, quiet_rig):
        prefix = "GET /launcher/status HTTP/1.1\r\n"
        ok = exchange(quiet_rig.sock_path, pad_head(prefix, HEAD_MAX))
        assert ok.status == 200
        too_big = exchange(quiet_rig.sock_path, pad_head(prefix, HEAD_MAX + 1))
        assert too_big.status == 431 and too_big.json() == {"code": "head_too_large"}

    def test_a_head_that_never_ends_is_cut_off_at_16_kib(self, quiet_rig):
        data = b"GET /launcher/status HTTP/1.1\r\nX-Pad: " + b"a" * (4 * HEAD_MAX)
        assert exchange(quiet_rig.sock_path, data).status == 431

    @pytest.mark.parametrize("size", [BODY_MAX + 1, 4294967296])
    def test_a_body_over_32_mib_is_refused_before_it_is_read(self, quiet_rig, size):
        head = f"POST /e/zimage/sdcpp/v1/img_gen HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: {size}\r\n\r\n"
        reply = exchange(quiet_rig.sock_path, head.encode())
        assert reply.status == 413 and reply.json() == {"code": "body_too_large"}

    def test_a_body_of_exactly_32_mib_goes_to_the_engine(self, launch):
        rig = launch()
        shell = b'{"prompt":"x","pad":""}'
        body = shell[:-2] + b"a" * (BODY_MAX - len(shell)) + shell[-2:]
        assert len(body) == BODY_MAX
        reply = exchange(
            rig.sock_path,
            request(
                "POST",
                "/e/zimage/sdcpp/v1/img_gen",
                body,
                ("Content-Type: application/json",),
            ),
            timeout=60,
        )
        assert reply.status == 202
        assert rig.engine_requests()[-1]["body_len"] == BODY_MAX


class TestSmuggling:
    def test_a_second_request_in_the_same_bytes_never_runs(self, launch):
        rig = launch()
        rig.submit("zimage")
        pipelined = request("GET", "/launcher/status") + request(
            "POST", "/launcher/kill"
        )
        reply = exchange(rig.sock_path, pipelined)
        assert reply.status == 200 and reply.json()["state"] == "ready"
        time.sleep(0.2)
        assert rig.status()["state"] == "ready" and len(rig.engines()) == 1

    def test_a_request_hidden_in_a_body_is_not_a_request(self, launch):
        rig = launch()
        inner = request("POST", "/sdcpp/v1/upscale", b"{}")
        body = b'{"prompt":"x"}'
        reply = exchange(
            rig.sock_path,
            request(
                "POST",
                "/e/zimage/sdcpp/v1/img_gen",
                body,
                ("Content-Type: application/json",),
            )
            + inner,
        )
        assert reply.status == 202
        # one request reached the engine, with exactly the declared body
        [seen] = rig.engine_requests()
        assert seen["body_len"] == len(body)
        assert seen["head"].startswith("POST /sdcpp/v1/img_gen ")
