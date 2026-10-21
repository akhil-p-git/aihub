#!/usr/bin/python3
"""A stand-in for sd-server's native /sdcpp/v1 API, for the launcher tests and the G1a gate.

The launcher starts it exactly as it would start sd-server. It listens on --listen-ip/--listen-port
(-l is the short form) and ignores every other argument, except these three:

    --standin-dir DIR     write DIR/pid, DIR/starts.log and DIR/requests.log (one JSON object per line)
    --standin-start MODE  how the start goes. The mode comes from models.json, because a prompt can only
                          arrive once the engine listens:
        exit3         print an error and exit with status 3 before listening, like a missing model file
        crash         abort (SIGABRT) before listening
        hang          never listen and never exit
        slow=SECONDS  "load the model" for that long before listening
        stubborn      ignore SIGTERM (noting it in DIR/signals.log), so that only SIGKILL stops it
        noisy         write about 20,000 log lines and two huge ones before listening

A keyword in the prompt of a job chooses how the job goes (the same style as tests/e2e/fake_providers.py):

    slowimage    one second per step
    imagefail    fail at step 2 with the lab's out-of-memory lines
    crashimage   abort (SIGABRT) at step 2, like the VAE crash seen in the lab
    noisyimage   flood stdout and stderr at step 1
    longimage    write one [I] line of 100,000 characters in two halves 0.3 s apart, the second half starting
                 like an error line (the middle of a line is not the start of one)
    logshapes    print every shape of engine line the lab's logs show, and two that only a hostile prompt
                 makes: a message of several lines (tag first, " --- file:line" last, here with an error
                 look-alike inside), ggml's lines without a file and line, a stray line, and a message that
                 never ends
    sentinel-X   log "[E] sentinel X" when the job starts, so that a test can wait until the launcher has
                 read everything printed before it
    fault-KIND   break the reply to the img_gen request: nolength, chunked, chunkedlength, twolengths,
                 hugehead, hugebody, truncated, stall, badstatus or garbage

The log lines have sd-server's shape: "[E] text --- file:line" on stderr, and [W], [I] and the rest on
stdout (examples/common/log.cpp at the pinned commit prints only errors to stderr). It logs the prompt of
every job at [I], as sd-server does at verbose level (which the launcher refuses, and where sd-server
escapes newlines; this does not), so a test can check that the launcher keeps it out of its own log.
The images are real WebP files (flat colour) of the sizes aihub asks for, and 64x64 when a request has no
size. Python 3.12 or newer, standard library only.
"""

import argparse
import base64
import hashlib
import json
import os
import queue
import re
import resource
import signal
import socketserver
import sys
import threading
import time
import zlib
from pathlib import Path

# zlib-compressed, base64 WebP files with one "VP8 " chunk, made once with Pillow:
#   Image.new("RGB", (w, h), (200, 90, 40)).save(buf, "WEBP", quality=90, method=0)
WEBP = {
    "64x64": (
        "eNoL8nRzy2BgYAh3dQoIC7BQiAGyP7AxzGXUcmBwYLAzlOh2UVJcKMjCoMDMsqXBmqEO"
        "CCVjJrH9/Gf+N+CXjfdMtcj2q/5iDD3bZBJXrBF4+CKngIHh31fz/f+/vpiyavr3Df//"
        "3zuYMOf3/+h5N0D2AAB/9Svu"
    ),
    "1024x1024": (
        "eNoL8nRzm8XLwBDu6hQQFmCh0AdkC6xlnMuoxcDCwGJnKNHjskhxoQALgwIzy5aGnEoH"
        "ewZ7Bp71c12K3Y/936E5zyI55rCjuMTkr5cLAgK+hyXwvdp8nCd7RobSY6G6XXlcXHKv"
        "zjDeDat42FvMyxKT5H/d96Cj893IButV52UM1yUu6O7x/PVyh4ZG9WoD9tLIx22mZ0wE"
        "CzvtY81ZWflL0xjerbYpOLazrUnPUP31sgcLFr5bc0D2anqfxHWDC67HVP6W3BERsb06"
        "g3nXmo+HpZ5Jcew4Mn/d7Kam9l2bGcpDMZzGGNl+Rpo75JLAgwSNwPRkxYhJ9rNPH+XS"
        "eZOiLeT+ubJioevcntYkj3/5+iIamzdOWOMpf/bpo1axMiPRTuWfe/YELDp3zGmjSr35"
        "9C6RKKnWKVlsvuJ3P66LiLqp3SWlv/txzIatm8JFTOLLj+1MzE5Zp5Gx731v0aFrJ2MC"
        "Ltbel3ralphw4d5GopW3rp20Y/GZpYejwpnVut/9uK6jmzP1KC9BCyv53yvyEq8cZGH+"
        "fJ7mEMKOWbjo9z7T09zRqoXObqY5RqteYpdz/V6TPU1sm9ejNhRXr1f8+0RxL9HKQa7W"
        "/fxwQhYOORw+Ora2LTSW6OAFO+OJ/Z4Wc+KVg1y9r8aINwhn4GDzkcTVDadWL8XrW3RX"
        "72n/PqGNeOUgV28/3+a4ipT0tXCx1DbJ0BJC0Y3q6gn/PgncI1450NXTfn44YepNKLpR"
        "XJ0cdlhrHbEJGOKMT/L3nKRJyI9AV9+3k2RbSUR6R7i6J+tGauglIhMwxBlxzr9POFcT"
        "rRzk6ujnhxWukpCDga7ujeZZtYv4HAxyxon6So584pUDXe1d8yNFainxORjoasPViVPD"
        "ScjBo0XYaBE2WoSNFmGjRdhoETZSirBFDAz//vpt//9vr9DUoH/f5P//fSPGeX7/1901"
        "jE8YGBgAY0kQaQ=="
    ),
    "1024x576": (
        "eNoL8nRz4+FgYAh3dQoIC7BQYACyA34zzGXUYmBxYLIzlOhxWaS4UICFQYGZZUtDTqWD"
        "PYM9A8/6uS7F7sf+79CcZ5Ecc9hRXGLy18sFAQHfwxL4Xm0+zpM9I0PpsVDdrjwuLrlX"
        "ZxjvhlU87C3mZYlJ8r/ue9DR+W5kg/Wq8zKG6xIXdPd4/nq5Q0OjerUBe2nk4zbTMyaC"
        "hZ32seasrPylaQzvVtsUHNvZ1qRnqP562YMFC9+tOSB7Nb1P4rrBBddjKn9L7oiI2F6d"
        "wbxrzcfDUs+kOHYcmb9udlNT+67NDOWhGE5jjGw/I80dckngQYJGYHqyYsQk+9mnj3Lp"
        "vEnRFnL/XFmx0HVuT2uSx798fRGNzRsnrPGUP/v0UatYmZFop/LPPXsCFp075rRRpd58"
        "epdIlFTrlCw2X/G7H9dFRN3U7pLS3/04ZsPWTeEiJvHlx3YmZqes08jY97636NC1kzEB"
        "F2vvSz1tS0y4cG8j0cpb107asfjM0sNR4cxq3e9+XNfRzZl6lJeghZX87xV5iVcOsjB/"
        "Pk9zCGHHLFz0e5/pae5o1UJnN9Mco1Uvscu5fq/Jnia2zetRG4qr1yv+faK4l2jlIFfr"
        "fn44IQuHHA4fHVvbFhpLdPCCnfHEfk+LOfHKQa7eV2PEG4QzcLD5SOLqhlOrl+L1Lbqr"
        "97R/n9BGvHKQq7efb3NcRUr6WrhYaptkaAmh6EZ19YR/nwTuEa8c6OppPz+cMPUmFN0o"
        "rk4OO6y1jtgEDHHGJ/l7TtIk5Eegq+/bSbKtJCK9I1zdk3UjNfQSkQkY4ow4598nnKuJ"
        "Vg5ydfTzwwpXScjBQFf3RvOs2kV8DgY540R9JUc+8cqBrvau+ZEitZT4HAx0teHqxKnh"
        "JOTg0SKMHkWYMgPDv79+2///2ys0NejfN/n/f9+IcZ7f/3V3DeMTBgYGAJzTIeI="
    ),
    "576x1024": (
        "eNoL8nRzY+NgYAh3dQoIC7BQ+MXOwDDhF8NcRi0HJgYWO0OJHpdFigsFWBgUmFm2NORU"
        "Otgz2DPwrJ/rUux+7P8OzXkWyTGHHcUlJn+9XBAQ8D0sge/V5uM82TMylB4L1e3K4+KS"
        "e3WG8W5YxcPeYl6WmCT/674HHZ3vRjZYrzovY7gucUF3j+evlzs0NKpXG7CXRj5uMz1j"
        "IljYaR9rzsrKX5rGIDqj5uCGrb6KfGxMrWJlRqKdyj/37AlYdO6Y00aVevPpXSKRN06E"
        "qfQ/+/TRaV6qntSlHZIm8eXHdiZmp6zTyNj3vrfo0LWTMQEXa+//Ljulwvq9Jnua2Dav"
        "R23d735c19HNmXqUV/zux3URaWK8vK+lnraunbRj4aLf+0xPc0erFjq7QpR71/xIkVqK"
        "Xc7rUb/RdN7MGJ5Z+rsfx2zYuilcBMX+v7tThZgwNUHsN+Zma/uKcCGa/ftqjHiDcMgV"
        "us/0btuyjs0X7Pqom9pdUsj2/7u7qZMRUxPEfsnWZucqbCEEVn7fTpJtJQ65HYvPLD0c"
        "Fc6shhZ6UPv/5910acDUBLGf59DBhXuTsYQQWHn+fJ7mEBxyMYHPgh6u1W6c7Io19v5v"
        "z1nkgD3KpZ62JSZcuLcRSwiBlW8/3+a4CofcOo3PmgXXph44uQhr7P2/vS1QAXuU9xYd"
        "NtiQkR+BJYTAyqOfH1a4ikMuXKRyyo5srwcpF7HGXv3b2xoTsEc50P4ZESbma7Cn76ib"
        "up8fTsjCIafdZXMqxnRpgVEG1tjb/+3tFA9urFEOtP+MjpT0NezpWzdn2s8PJ0y9sctN"
        "PSo3mmFHM+xohqVzhmVg+PfXb/v/f3uFpgb9+yb//+8bMc7z+7/urmF8wsDAAAA3MyCL"
    ),
}
# A 1x1 PNG, the shape of the preview frame the real server sends with "preview": "proj".
PREVIEW = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGM4EaUBAAM4AUu6yANpAAAAAElFTkSuQmCC"

JOB_PATH = re.compile(r"/sdcpp/v1/jobs/([A-Za-z0-9_-]{1,64})")
CANCEL_PATH = re.compile(r"/sdcpp/v1/jobs/([A-Za-z0-9_-]{1,64})/cancel")

LOCK = threading.Lock()  # guards JOBS and the two log files
JOBS: dict[str, dict] = {}
PENDING: "queue.Queue[tuple[str, dict]]" = (
    queue.Queue()
)  # the server makes one image at a time
RECORD_DIR: "Path | None" = None


def log(level: str, text: str) -> None:
    stream = sys.stderr if level == "E" else sys.stdout
    stream.write(f"[{level}] {text} --- fake_sd_server.py:1\n")
    stream.flush()


def ggml_log(level: str, text: str) -> None:
    """ggml's own messages reach the log through the same callback, tagged but without " --- file:line"."""
    stream = sys.stderr if level == "E" else sys.stdout
    stream.write(f"[{level}] ggml: {text}\n")
    stream.flush()


def record(name: str, entry: dict) -> None:
    if RECORD_DIR is not None:
        with LOCK, (RECORD_DIR / name).open("a") as out:
            out.write(json.dumps(entry) + "\n")


def crash() -> None:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))  # no core file for a test
    os.abort()


def flood() -> None:
    for number in range(5000):
        log("I", f"loading tensor {number}")
        log("W", f"warning number {number}")
        log("E", f"error number {number}")
        sys.stdout.write(f"untagged line {number}\n")
    log("E", "x" * 300_000)  # one line far longer than any log line sd-server writes
    sys.stderr.write("y" * 300_000 + "\n")
    log("E", "z" * 5000)  # long, but inside what the launcher reads of a line
    log("E", "last error line")


def long_line() -> None:
    sys.stdout.write("[I] " + "a" * 100_000)
    sys.stdout.flush()
    time.sleep(0.3)
    sys.stdout.write("[E] LEAKMARK-02 second half of a long line\n")
    sys.stdout.flush()


def log_shapes() -> None:
    def raw(text: str) -> None:
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    # A message of several lines, as sd-server prints a multi-line string: the tag on the first line and
    # " --- file:line" at the end of the last.
    raw("[I] a message that goes on")
    raw("[E] a line that only looks like an error")
    raw("and a last line --- fake_sd_server.py:1")
    # ggml's lines come through the same callback, without the file and line.
    ggml_log("W", "a warning from ggml")
    ggml_log("I", "an info line from ggml")
    log("E", "kept after a ggml line")
    ggml_log("E", "an error from ggml")
    raw("a stray line from a library")
    raw("[I] a message that never ends")
    for number in range(60):
        raw(f"[E] line {number} of a message that never ends")
    log("E", "kept after the long message")


def start_mode(mode: str) -> None:
    name, _, value = mode.partition("=")
    if name == "exit3":
        log("W", "backend CUDA0 reported 0 MB free")
        log(
            "E",
            "cannot inspect model source 'z-image-turbo/z_image_turbo-Q5_0.gguf': No such file or directory",
        )
        sys.exit(3)
    elif name == "crash":
        log("E", "ggml_cuda_init failed")
        crash()
    elif name == "hang":
        while True:
            time.sleep(3600)
    elif name == "slow":
        log("I", "loading model")
        time.sleep(float(value))
    elif name == "stubborn":
        # Note each SIGTERM in DIR/signals.log, so that a test can wait until the launcher has sent one.
        signal.signal(signal.SIGTERM, lambda *_: record("signals.log", {"sig": "TERM"}))
    elif name == "noisy":
        flood()
    elif name:
        sys.exit(f"unknown --standin-start mode {mode!r}")


class Handler(socketserver.StreamRequestHandler):
    timeout = 120

    def handle(self) -> None:
        head = b""
        while True:
            line = self.rfile.readline(70_000)
            head += line
            if line in (b"\r\n", b"\n", b""):
                break
        if not head.strip():
            return
        request_line, *header_lines = head.decode("latin-1").split("\r\n")
        method, target, _ = request_line.split(" ")
        headers = {}
        for header in header_lines:
            name, _, value = header.partition(":")
            headers[name.strip().lower()] = value.strip()
        body = self.rfile.read(int(headers.get("content-length", 0)))
        record(
            "requests.log",
            {
                "head": head.decode("latin-1"),
                "body_len": len(body),
                "body_sha256": hashlib.sha256(body).hexdigest(),
            },
        )
        if method == "POST" and target == "/sdcpp/v1/img_gen":
            self.submit(body)
        elif method == "GET" and (match := JOB_PATH.fullmatch(target)):
            self.poll(match[1])
        elif method == "POST" and (match := CANCEL_PATH.fullmatch(target)):
            self.cancel(match[1])
        else:
            self.send(404, {"error": "not found"})

    def send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.wfile.write(
            f"HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )

    def submit(self, body: bytes) -> None:
        try:
            request = json.loads(body)
            prompt = request["prompt"]
            size = f"{request.get('width', 64)}x{request.get('height', 64)}"
            if not isinstance(prompt, str) or size not in WEBP:
                raise ValueError(f"no stand-in image of size {size}")
        except (ValueError, KeyError, TypeError) as err:
            self.send(400, {"error": f"bad request: {err}"})
            return
        job_id = "job_" + os.urandom(8).hex()
        job = {
            "id": job_id,
            "kind": "img_gen",
            "status": "queued",
            "created": int(time.time()),
            "started": None,
            "completed": None,
            "queue_position": 0,
            "result": None,
            "error": None,
            "preview": None,
        }
        with LOCK:
            JOBS[job_id] = job
        PENDING.put((job_id, request))
        reply = {
            "id": job_id,
            "kind": "img_gen",
            "status": "queued",
            "created": job["created"],
            "poll_url": f"/sdcpp/v1/jobs/{job_id}",
        }
        fault = re.search(r"fault-([a-z]+)", prompt.lower())
        if fault:
            self.fault(fault[1], json.dumps(reply).encode())
        else:
            self.send(202, reply)

    def poll(self, job_id: str) -> None:
        with LOCK:
            job = JOBS.get(job_id)
            if job is None:
                snapshot = None
            else:
                waiting = [i for i, j in JOBS.items() if j["status"] == "queued"]
                job["queue_position"] = (
                    waiting.index(job_id) + 1 if job_id in waiting else 0
                )
                snapshot = dict(job)
        if snapshot is None:
            self.send(404, {"error": "no such job"})
        else:
            self.send(200, snapshot)

    def cancel(self, job_id: str) -> None:
        with LOCK:
            job = JOBS.get(job_id)
            if job is not None and job["status"] == "queued":
                job.update(
                    status="cancelled",
                    completed=int(time.time()),
                    error={"code": "cancelled", "message": "job cancelled by client"},
                )
        if job is None:
            self.send(404, {"error": "no such job"})
        elif job["status"] == "cancelled":
            self.send(200, {"id": job_id, "status": "cancelled"})
        else:
            self.send(409, {"error": f"job is {job['status']}"})

    def fault(self, kind: str, body: bytes) -> None:
        """Answer the img_gen request in a way the launcher must not pass on."""
        head = "HTTP/1.1 202 Accepted\r\nContent-Type: application/json\r\n"
        n = len(body)
        if kind == "nolength":
            self.wfile.write((head + "Connection: close\r\n\r\n").encode() + body)
        elif kind == "chunked":
            self.wfile.write(
                (head + "Transfer-Encoding: chunked\r\n\r\n").encode()
                + f"{n:x}\r\n".encode()
                + body
                + b"\r\n0\r\n\r\n"
            )
        elif kind == "chunkedlength":
            # Both framings at once: a parser that trusts one reads a different message than one that trusts the other
            self.wfile.write(
                (
                    head + f"Transfer-Encoding: chunked\r\nContent-Length: {n}\r\n\r\n"
                ).encode()
                + f"{n:x}\r\n".encode()
                + body
                + b"\r\n0\r\n\r\n"
            )
        elif kind == "twolengths":
            self.wfile.write(
                (
                    head + f"Content-Length: {n}\r\nContent-Length: {n + 1}\r\n\r\n"
                ).encode()
                + body
            )
        elif kind == "hugehead":
            self.wfile.write(
                (
                    head + f"X-Pad: {'a' * 20_000}\r\nContent-Length: {n}\r\n\r\n"
                ).encode()
                + body
            )
        elif kind == "hugebody":
            self.wfile.write(
                (head + f"Content-Length: {20 * 1024 * 1024}\r\n\r\n").encode()
            )
            try:
                for _ in range(20):
                    self.wfile.write(b"0" * 1024 * 1024)
                    self.wfile.flush()
            except OSError:
                pass  # the launcher stopped reading
        elif kind == "truncated":
            self.wfile.write(
                (head + f"Content-Length: {n + 100}\r\n\r\n").encode() + body
            )
        elif kind == "stall":
            self.wfile.write(
                (head + f"Content-Length: {n}\r\n\r\n").encode() + body[: n // 2]
            )
            self.wfile.flush()
            time.sleep(60)
        elif kind == "badstatus":
            self.wfile.write(b"HTTP/1.1 9999 What\r\nContent-Length: 0\r\n\r\n")
        else:
            self.wfile.write(b"\x00\x01 this is not http\r\n\r\n")


def work() -> None:
    while True:
        job_id, request = PENDING.get()
        with LOCK:
            job = JOBS[job_id]
            if job["status"] != "queued":
                continue  # cancelled while it waited
            job.update(status="generating", started=int(time.time()), queue_position=0)
        prompt = request["prompt"]
        words = prompt.lower()
        log("I", f"prompt: {prompt}")
        if mark := re.search(r"sentinel-([a-z0-9]+)", words):
            log(
                "E", f"sentinel {mark[1]}"
            )  # a line a test waits for: all before it have been read
        steps = int((request.get("sample_params") or {}).get("sample_steps") or 4)
        for step in range(1, steps + 1):
            time.sleep(1.0 if "slowimage" in words else 0.05)
            with LOCK:
                if request.get("preview", "none") != "none":
                    job["preview"] = {
                        "pass": 1,
                        "step": step,
                        "total_steps": steps,
                        "b64_json": PREVIEW,
                    }
            if "noisyimage" in words and step == 1:
                flood()
            if "longimage" in words and step == 1:
                long_line()
            if "logshapes" in words and step == 1:
                log_shapes()
            if "crashimage" in words and step == 2:
                log("E", "VAE decode failed")
                crash()
            if "imagefail" in words and step == 2:
                log(
                    "W",
                    "model manager cannot make enough memory available on CUDA0: need 2129.16 MB device / 1617.16 MB budget, available 950.31 MB device / unlimited budget",
                )
                ggml_log(
                    "E",
                    "ggml_backend_cuda_buffer_type_alloc_buffer: allocating 1021.54 MiB on device 0: cudaMalloc failed: CUDA-capable device(s) is/are busy or unavailable",
                )
                log("E", "generate failed")
                with LOCK:
                    job.update(
                        status="failed",
                        completed=int(time.time()),
                        error={
                            "code": "generation_failed",
                            "message": "generate_image returned empty results",
                        },
                    )
                break
        else:
            size = f"{request.get('width', 64)}x{request.get('height', 64)}"
            image = base64.b64encode(
                zlib.decompress(base64.b64decode(WEBP[size]))
            ).decode()
            with LOCK:
                job.update(
                    status="completed",
                    completed=int(time.time()),
                    result={
                        "output_format": "webp",
                        "images": [{"index": 0, "b64_json": image}],
                    },
                )


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> None:
    global RECORD_DIR
    parser = argparse.ArgumentParser(allow_abbrev=False, add_help=False)
    parser.add_argument("--listen-ip", "-l", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, default=1234)
    parser.add_argument("--standin-dir")
    parser.add_argument("--standin-start", default="")
    args, _ = parser.parse_known_args()  # sd-server's other arguments mean nothing here
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if args.standin_dir:
        RECORD_DIR = Path(args.standin_dir)
        (RECORD_DIR / "pid").write_text(str(os.getpid()))
        try:
            os.fstat(3)
            fd3_open = True
        except OSError:
            fd3_open = False
        listen_vars = {k: v for k, v in os.environ.items() if k.startswith("LISTEN_")}
        record(
            "starts.log",
            {
                "pid": os.getpid(),
                "argv": sys.argv[1:],
                "listen_vars": listen_vars,
                "fd3_open": fd3_open,
                "env": dict(os.environ),
            },
        )
    start_mode(args.standin_start)
    threading.Thread(target=work, daemon=True).start()
    with Server((args.listen_ip, args.listen_port), Handler) as server:
        log("I", f"listening on: http://{args.listen_ip}:{args.listen_port}")
        server.serve_forever()


if __name__ == "__main__":
    main()
