"""The image engine's launcher, faked, for the browser tests. Usage: fake_engine.py SOCKET CONTROL_PORT

It speaks the launcher's protocol on a Unix socket with the FakeEngine the Python tests use, and offers a control
API on 127.0.0.1:CONTROL_PORT so that a test can see what the engine was asked and let a held image move on:

    GET  /__calls       [[method, path], ...] for every request the engine got
    GET  /__submitted   the img_gen request bodies, in order
    POST /__release     let held images take one more step (?count=N: N steps)

A word in the prompt chooses how an image goes, as in fake_providers.py:

    holdimage   reports step 1, then waits for /__release before each further step and before the image
    failimage   the engine runs out of GPU memory at step 1 (the lab's own log lines)
    (anything else) the usual steps, then the image
"""

import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/, for helpers
from helpers import (
    HOLD,
    FakeEngine,
    engine_errors,
    failed,
    finished,
    generating,
    queued,
    serve_engine,
)


class KeywordEngine(FakeEngine):
    def _submit(self, model: str, raw: bytes) -> tuple[int, object]:
        body = json.loads(raw)
        prompt = body.get("prompt", "")
        steps = body.get("sample_params", {}).get("sample_steps", 4)
        if "holdimage" in prompt:
            script: list = [queued(1), generating(1, steps)]
            for step in range(2, steps + 1):
                script += [HOLD, generating(step, steps)]
            self.scripts.append([*script, HOLD, finished])
        elif "failimage" in prompt:
            self.last_error = engine_errors("vram_short")
            self.scripts.append([queued(1), generating(1, steps), failed()])
        return super()._submit(model, raw)


def control(engine: FakeEngine, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def reply(self, payload: object) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/__calls":
                self.reply([list(call) for call in engine.calls])
            elif self.path == "/__submitted":
                self.reply(engine.submitted)
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            url = urlsplit(self.path)
            if url.path != "/__release":
                return self.send_error(404)
            count = int(parse_qs(url.query).get("count", ["1"])[0])
            engine.release(count)
            self.reply({"released": count})

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    socket_path, control_port = sys.argv[1], int(sys.argv[2])
    # So that the socket is closed on the way out.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    fake = KeywordEngine()
    server = control(fake, control_port)
    with serve_engine(fake, socket_path):
        try:
            threading.Event().wait()
        finally:
            server.shutdown()
