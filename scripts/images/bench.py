#!/usr/bin/env python3
"""Times the image engine through its launcher socket, the way aihub-web calls it.

    python3 scripts/images/bench.py --out ~/work/aihub-image-lab/samples/pinned

For each model, prompt and shape it makes `--runs` images with seed 1234 and writes `<model>_<prompt>_<shape>_run<N>.webp`
and a `.txt` sidecar (wall seconds, VRAM before and at peak, sampled every 200 ms) into `--out`. The first image
after the engine starts or the model changes is cold (it loads the model); the sidecar says so. Prints a table.

It takes the GPU lock (an exclusive flock on the socket's directory, the same one aihub-web uses) for the whole
run and gives up if it cannot get it within 120 s, so it never runs beside a chat reply or an image of aihub-web's.
It does not unload Ollama: with a model resident the engine may fail for lack of VRAM, and the failure is reported.
Standard library only; the request fields are the ones in app/aihub_web/providers/images.py.
"""

import argparse
import base64
import fcntl
import http.client
import json
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

SOCKET = f"/run/user/{os.getuid()}/aihub-images/engine.sock"
SEED = 1234
# The model ids and sampler settings of config/web.example.yaml.
MODELS = {"zimage": (8, None), "klein4b": (4, "euler")}
# The shapes aihub offers; Wide and Tall hold the pixel count of 768 square, so no job exceeds the 1024 square.
SHAPES = {"square": (1024, 1024), "wide": (1024, 576), "tall": (576, 1024)}
PROMPTS = {
    "A": "a red fox sitting in fresh snow at sunrise, soft golden light, photorealistic, shallow depth of field",
    "B": "a neon sign reading OPEN LATE above a rainy city street at night, cinematic",
    "C": "flat vector illustration of a mountain cabin beside a lake, limited warm palette",
}


class Unix(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float) -> None:
        super().__init__("engine", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def call(
    method: str, path: str, body: dict | None = None, timeout: float = 130.0
) -> tuple[int, dict]:
    conn = Unix(SOCKET, timeout)
    try:
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Connection": "close"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read(32 * 1024 * 1024)
        try:
            return resp.status, json.loads(raw)
        except ValueError:
            return resp.status, {}
    finally:
        conn.close()


class Vram:
    """The GPU's used memory, sampled every 200 ms by one nvidia-smi process."""

    def __init__(self) -> None:
        self._proc = subprocess.Popen(
            [
                "nvidia-smi",
                "--query-gpu=memory.used",
                "--format=csv,noheader,nounits",
                "-lms",
                "200",
            ],
            stdout=subprocess.PIPE,
            text=True,
        )
        self.now = 0
        self.peak = 0
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            try:
                self.now = int(line.strip())
            except ValueError:
                continue
            self.peak = max(self.peak, self.now)

    def reset(self) -> int:
        """Start a new window; returns the level at its start."""
        self.peak = self.now
        return self.now

    def close(self) -> None:
        self._proc.terminate()
        self._proc.wait()


def one_image(
    model: str, prompt: str, width: int, height: int, timeout_s: float
) -> tuple[bytes | None, str]:
    steps, method = MODELS[model]
    params: dict[str, object] = {"sample_steps": steps, "guidance": {"txt_cfg": 1.0}}
    if method:
        params["sample_method"] = method
    status, body = call(
        "POST",
        f"/e/{model}/sdcpp/v1/img_gen",
        {
            "prompt": prompt,
            "width": width,
            "height": height,
            "seed": SEED,
            "batch_count": 1,
            "sample_params": params,
            "output_format": "webp",
            "output_compression": 90,
            "embed_image_metadata": False,
        },
    )
    if not 200 <= status < 300 or "id" not in body:
        return None, f"submit {status}: {json.dumps(body)[:300]}"
    job = body["id"]
    give_up = time.monotonic() + timeout_s
    while time.monotonic() < give_up:
        status, body = call("GET", f"/e/{model}/sdcpp/v1/jobs/{job}", timeout=15)
        state = body.get("status")
        if state == "completed":
            return base64.b64decode(
                body["result"]["images"][0]["b64_json"]
            ), "completed"
        if state in ("failed", "cancelled") or status in (404, 410, 503):
            return (
                None,
                f"{state or status}: {json.dumps(body.get('error') or body.get('last_error'))[:300]}",
            )
        time.sleep(0.25)
    call("POST", "/launcher/kill")
    return None, "timeout (engine killed)"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument("--models", nargs="+", choices=MODELS, default=list(MODELS))
    ap.add_argument("--shapes", nargs="+", choices=SHAPES, default=list(SHAPES))
    ap.add_argument("--prompts", nargs="+", choices=PROMPTS, default=list(PROMPTS))
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument(
        "--timeout", type=float, default=240.0, help="seconds to wait for one image"
    )
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    lock_fd = os.open(os.path.dirname(SOCKET), os.O_RDONLY | os.O_DIRECTORY)
    give_up = time.monotonic() + 120
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() > give_up:
                print(
                    "bench: the GPU lock is held (aihub-web is making an image or a chat reply); try later",
                    file=sys.stderr,
                )
                return 2
            time.sleep(0.25)

    vram = Vram()
    time.sleep(0.6)
    rows: list[tuple[str, str, str, str, float | None, int, int]] = []
    failures = 0
    try:
        loaded = call("GET", "/launcher/status")[1].get("engine")
        for model in args.models:
            for shape in args.shapes:
                width, height = SHAPES[shape]
                for pid in args.prompts:
                    for run in range(1, args.runs + 1):
                        cold = loaded != model
                        before = vram.reset()
                        t0 = time.monotonic()
                        image, outcome = one_image(
                            model, PROMPTS[pid], width, height, args.timeout
                        )
                        wall = time.monotonic() - t0
                        loaded = model if image else None
                        stem = args.out / f"{model}_{pid}_{shape}_run{run}"
                        if image:
                            stem.with_suffix(".webp").write_bytes(image)
                        else:
                            failures += 1
                        stem.with_suffix(".txt").write_text(
                            f"prompt: {PROMPTS[pid]}\nseed: {SEED}\nmodel: {model}\nresolution: {width}x{height}\n"
                            f"cold: {cold}\nwall_s: {wall:.2f}\noutcome: {outcome}\n"
                            f"gpu_before_mib: {before}\ngpu_peak_mib: {vram.peak}\ngpu_peak_delta_mib: {vram.peak - before}\n"
                        )
                        rows.append(
                            (
                                model,
                                shape,
                                pid,
                                "cold" if cold else "warm",
                                wall if image else None,
                                vram.peak,
                                run,
                            )
                        )
                        print(
                            f"{model:8} {shape:6} {pid} run{run} {'cold' if cold else 'warm'} "
                            f"{wall:6.1f}s peak {vram.peak} MiB  {outcome}",
                            flush=True,
                        )
    finally:
        vram.close()
        os.close(lock_fd)

    print("\nwarm median seconds (peak VRAM MiB):")
    for model in args.models:
        for shape in args.shapes:
            warm = [
                r[4]
                for r in rows
                if r[0] == model
                and r[1] == shape
                and r[3] == "warm"
                and r[4] is not None
            ]
            peaks = [r[5] for r in rows if r[0] == model and r[1] == shape]
            if warm:
                print(
                    f"  {model:8} {shape:6} {statistics.median(warm):6.1f} s  ({max(peaks)} MiB, n={len(warm)})"
                )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
