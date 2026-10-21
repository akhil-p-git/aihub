"""The image engine's launcher, spoken to over its Unix socket: the one place that knows the wire format.

The launcher forwards `/e/<model>/sdcpp/v1/...` to sd-server and answers `/launcher/...` itself. It serves one
request per connection, so every call opens a client of its own and sends `Connection: close`. Whatever comes
back is untrusted: replies are capped, ids are checked, and the `poll_url` in an answer is never followed.
"""

import base64
import json
import re
from dataclasses import dataclass

import httpx2

from .base import ImageRequest

MAX_REPLY_BYTES = 16 * 1024 * 1024
# Lossy WebP quality asked of the engine: about 0.1 to 0.2 MB an image.
WEBP_QUALITY = 90
MAX_LOG_LINES = 20
MAX_LOG_LINE = 500
MODEL_ID = re.compile(r"[a-z0-9-]{1,32}")
JOB_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
JOB_STATES = ("queued", "generating", "completed", "failed", "cancelled")
ENGINE_STATES = ("idle", "starting", "ready", "failed")
HEADERS = {"Connection": "close", "Accept-Encoding": "identity"}
QUICK = httpx2.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
# A submit waits while the launcher starts the engine and loads the model, which it gives 90 s.
SUBMIT = httpx2.Timeout(connect=5.0, read=120.0, write=30.0, pool=5.0)


class EngineError(Exception):
    """A call to the launcher failed. `code` says how; `lines` are the engine's last [E]/[W] log lines."""

    def __init__(
        self, code: str, message: str = "", lines: tuple[str, ...] = ()
    ) -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message
        self.lines = lines


@dataclass(frozen=True, slots=True)
class EngineStatus:
    engine: str | None  # the model whose engine is loaded
    state: str  # idle, starting, ready or failed
    last_error: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class JobState:
    status: str  # queued, generating, completed, failed or cancelled
    queue_position: int = 0
    step: int | None = None  # sampling progress, when the engine reports it
    total_steps: int | None = None
    image_b64: str | None = None  # the finished image, still encoded
    error_code: str | None = None
    error_message: str | None = None


def _protocol(message: str) -> EngineError:
    return EngineError("engine_protocol", message)


def _log_lines(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    lines = [line[:MAX_LOG_LINE] for line in value if isinstance(line, str)]
    return tuple(lines[:MAX_LOG_LINES])


def _count(value: object, what: str) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 100_000
    ):
        raise _protocol(f"The image engine reported a bad {what}.")
    return value


def _fail(status: int, data: object, *, job: bool) -> EngineError:
    """The error for a reply that is not a success. Launcher replies carry a `code`, the engine's an `error`."""
    body = data if isinstance(data, dict) else {}
    if status == 503 and body.get("code") == "engine_failed_to_start":
        return EngineError(
            "engine_failed_to_start", lines=_log_lines(body.get("last_error"))
        )
    if job and status in (404, 410):
        return EngineError("engine_gone", lines=_log_lines(body.get("last_error")))
    if status == 404 and body.get("code") == "unknown_model":
        return EngineError("unknown_model")
    if status in (502, 504):
        # The launcher could not talk to the engine, or it did not answer: the engine is broken.
        return EngineError(
            "engine_io",
            "The image engine did not answer properly.",
            _log_lines(body.get("last_error")),
        )
    text = body.get("error")
    if status == 400:
        return EngineError("bad_request", text[:300] if isinstance(text, str) else "")
    if status == 429:
        return EngineError("engine_busy", "The image engine's queue is full.")
    return _protocol(f"The image engine answered {status}.")


def _img_gen_body(req: ImageRequest) -> dict[str, object]:
    params: dict[str, object] = {"sample_steps": req.steps}
    if req.sample_method:
        params["sample_method"] = req.sample_method
    params["guidance"] = {"txt_cfg": 1.0}
    body: dict[str, object] = {
        "prompt": req.prompt,
        "width": req.width,
        "height": req.height,
        "seed": req.seed,
        "batch_count": 1,
        "sample_params": params,
        # Previews only so that polls carry the step; the frame itself is dropped on arrival.
        "preview": "proj",
        "output_format": "webp",
        "output_compression": WEBP_QUALITY,
        # Off, so the prompt is not written into the file as XMP.
        "embed_image_metadata": False,
    }
    if req.reference_webp is not None:
        body["ref_images"] = [base64.b64encode(req.reference_webp).decode("ascii")]
    return body


def _job_state(body: dict[str, object]) -> JobState:
    status = body.get("status")
    if status not in JOB_STATES:
        raise _protocol("The image engine reported an unknown job state.")
    position = (
        _count(body["queue_position"], "queue position")
        if "queue_position" in body
        else 0
    )
    step = total = image = code = message = None
    preview = body.get("preview")
    if isinstance(preview, dict) and "step" in preview:
        step = _count(preview["step"], "step")
        total = (
            _count(preview["total_steps"], "step count")
            if "total_steps" in preview
            else None
        )
    if status == "completed":
        result = body.get("result")
        images = result.get("images") if isinstance(result, dict) else None
        first = images[0] if isinstance(images, list) and images else None
        image = first.get("b64_json") if isinstance(first, dict) else None
        if not isinstance(image, str):
            raise _protocol("The image engine finished without an image.")
    error = body.get("error")
    if isinstance(error, dict):
        code, message = (
            str(error.get("code", ""))[:100],
            str(error.get("message", ""))[:300],
        )
    return JobState(str(status), position, step, total, image, code, message)


class EngineClient:
    def __init__(
        self, socket: str, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        """`transport` replaces the Unix socket, for tests."""
        self._socket = socket
        self._transport = transport

    async def _call(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        timeout: httpx2.Timeout = QUICK,
    ) -> tuple[int, object]:
        """(status, decoded JSON or None). The reply is read in pieces and dropped past the cap."""
        transport = self._transport or httpx2.AsyncHTTPTransport(uds=self._socket)
        chunks: list[bytes] = []
        size = 0
        try:
            async with (
                httpx2.AsyncClient(
                    transport=transport,
                    base_url="http://engine",
                    headers=HEADERS,
                    timeout=timeout,
                ) as client,
                client.stream(method, path, json=body) as response,
            ):
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_REPLY_BYTES:
                        raise _protocol(
                            "The image engine sent a reply larger than 16 MiB."
                        )
                    chunks.append(chunk)
        except httpx2.ConnectError as err:
            raise EngineError(
                "engine_unreachable", "Can't connect to the image engine."
            ) from err
        except httpx2.TimeoutException as err:
            raise EngineError(
                "engine_timeout", "The image engine did not answer in time."
            ) from err
        except httpx2.TransportError as err:
            raise EngineError(
                "engine_io", "The image engine closed the connection."
            ) from err
        try:
            data = json.loads(b"".join(chunks))
        except ValueError:
            data = None
        return response.status_code, data

    @staticmethod
    def _ok(status: int, data: object, *, job: bool = False) -> dict[str, object]:
        if not 200 <= status < 300:
            raise _fail(status, data, job=job)
        if not isinstance(data, dict):
            raise _protocol("The image engine sent something that isn't a JSON object.")
        return data

    @staticmethod
    def _check(model_id: str, job_id: str | None = None) -> None:
        if not MODEL_ID.fullmatch(model_id):
            raise _protocol("That is not a model id.")
        if job_id is not None and not JOB_ID.fullmatch(job_id):
            raise _protocol("That is not a job id.")

    async def status(self) -> EngineStatus:
        """Which engine is loaded and how it is doing. Never starts one."""
        body = self._ok(*await self._call("GET", "/launcher/status"))
        engine, state, lines = (
            body.get("engine"),
            body.get("state"),
            body.get("last_error"),
        )
        if not (
            engine is None or isinstance(engine, str) and MODEL_ID.fullmatch(engine)
        ):
            raise _protocol("The image engine named a model that isn't an id.")
        if state not in ENGINE_STATES or not isinstance(lines, list):
            raise _protocol("The image engine's status makes no sense.")
        return EngineStatus(engine, str(state), _log_lines(lines))

    async def submit(self, req: ImageRequest) -> str:
        """Start a job, which may first start the engine; returns the engine's id for the job."""
        self._check(req.model_id)
        path = f"/e/{req.model_id}/sdcpp/v1/img_gen"
        body = self._ok(*await self._call("POST", path, _img_gen_body(req), SUBMIT))
        job_id = body.get("id")
        if not isinstance(job_id, str) or not JOB_ID.fullmatch(job_id):
            raise _protocol("The image engine named a job that isn't an id.")
        return job_id

    async def poll(self, model_id: str, job_id: str) -> JobState:
        self._check(model_id, job_id)
        status, data = await self._call("GET", f"/e/{model_id}/sdcpp/v1/jobs/{job_id}")
        return _job_state(self._ok(status, data, job=True))

    async def cancel(self, model_id: str, job_id: str) -> bool:
        """Cancel a queued job. False when it can't be: it is already generating, or the engine is gone."""
        self._check(model_id, job_id)
        status, data = await self._call(
            "POST", f"/e/{model_id}/sdcpp/v1/jobs/{job_id}/cancel"
        )
        if status == 200:
            return True
        if status in (404, 409, 410):
            return False
        raise _fail(status, data, job=True)

    async def kill(self) -> bool:
        """Kill the running engine, whatever it is doing. True when there was one."""
        body = self._ok(*await self._call("POST", "/launcher/kill"))
        return body.get("killed") is True
