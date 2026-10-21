"""Making images: checks on what the engine returns, one lock for the GPU, and jobs that outlive their request."""

import asyncio
import base64
import binascii
import fcntl
import inspect
import logging
import os
import re
import sqlite3
import struct
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

import httpx2

from .config import OllamaConfig
from .db import ImageRow, Message, Store
from .providers.base import ImageRequest, Progress
from .providers.images import EngineClient, EngineError

log = logging.getLogger(__name__)

# Shapes are all 1024-class: the same pixel count as the 768 px square for Wide and Tall.
SHAPES = {"square": (1024, 1024), "wide": (1024, 576), "tall": (576, 1024)}
MAX_PROMPT_CHARS = 2000
MAX_IMAGE_BYTES = 8 * 1024 * 1024
IMAGE_STORE_CAP = 2 * 1024**3
# How long an image waits for the GPU, and how often it looks again.
LOCK_WAIT = 120.0
LOCK_TICK = 0.25
# How long Ollama gets to let go of its models before an image is refused.
UNLOAD_WAIT = 5.0
# No image may hold the GPU longer than this, whatever its estimate says.
DEADLINE_CAP = 300.0

# What the user is told, per error code. The wording is the one the UI is written against.
MESSAGES = {
    "vram_busy": "The GPU is short of memory ({free} MB free). Close GPU-heavy apps or choose Wide or Tall, then retry.",
    "vram_busy_unknown": "The GPU is short of memory. Close GPU-heavy apps or choose Wide or Tall, then retry.",
    "cuda_unavailable": "The GPU didn't respond. If this keeps happening, restart the PC.",
    "gpu_paused": "The GPU is making an image, so local models are paused until it finishes. Cloud models still work.",
    "gpu_busy": "The GPU stayed busy for {minutes:g} minutes with other replies or images. Try again in a moment.",
    "ollama_busy": "A local model ({name}) is still loaded and won't unload. Wait for its reply to finish, then retry.",
    "engine_unreachable": "The image engine isn't running. Run systemctl --user reset-failed aihub-images.socket aihub-images.service, then systemctl --user start aihub-images.socket.",
    "engine_crashed": "The image engine stopped unexpectedly. Retry starts it again.",
    "engine_failed": "The image engine reported an error.",
    "timeout": "The image took too long, so it was stopped. Retry, or choose a faster model or Wide or Tall.",
    "internal": "The server hit an unexpected error. The service log has the details.",
}


class ImageError(Exception):
    """An image could not be made. `code` picks what the UI offers next; `message` is shown as written."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class Busy(Exception):
    """The GPU can't take this right now. The message says why and is shown as written."""


def _plain_webp_error(reason: str) -> ImageError:
    return ImageError(
        "bad_output",
        f"The image engine returned a file that isn't a plain WebP image ({reason}).",
    )


def validate_webp(data: bytes, width: int, height: int) -> bytes:
    """Return `data` if it is exactly what a lossy, metadata-free WebP of this size looks like.

    The engine's output is untrusted. The only shape accepted is RIFF/WEBP holding a single `VP8 ` chunk
    whose frame is a visible key frame of the requested size, with nothing after it: no VP8X header, no
    XMP (which would carry the prompt), EXIF or ICC chunk, no alpha, no animation.
    """
    if len(data) > MAX_IMAGE_BYTES:
        raise _plain_webp_error("it is larger than 8 MiB")
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise _plain_webp_error("it is not a RIFF/WebP file")
    claimed = struct.unpack_from("<I", data, 4)[0] + 8
    if claimed < len(data):
        raise _plain_webp_error("it has trailing bytes after the image")
    if claimed > len(data):
        raise _plain_webp_error("it is shorter than its header says")
    tag = data[12:16]
    if tag != b"VP8 ":
        what = (
            "extended header, which means metadata or an alpha channel"
            if tag == b"VP8X"
            else "not a lossy VP8 image"
        )
        raise _plain_webp_error(
            f"it starts with a {tag.decode('ascii', 'replace')!r} chunk, {what}"
        )
    size = struct.unpack_from("<I", data, 16)[0]
    if 20 + size + (size & 1) != len(data):
        raise _plain_webp_error("it has more than one chunk, such as embedded metadata")
    # The 10-byte frame header, as libwebp's VP8GetInfo reads it.
    frame = data[20 : 20 + size]
    if len(frame) < 10 or frame[3:6] != b"\x9d\x01\x2a":
        raise _plain_webp_error("the VP8 frame header is damaged")
    flags = frame[0] | frame[1] << 8 | frame[2] << 16
    key_frame, profile, visible, first_partition = (
        not flags & 1,
        flags >> 1 & 7,
        flags >> 4 & 1,
        flags >> 5,
    )
    if not (key_frame and profile <= 3 and visible and first_partition < size):
        raise _plain_webp_error("the VP8 frame is not a visible key frame")
    # Whole 16-bit fields are compared: their two high bits are an upscaling hint that is never set.
    found = struct.unpack_from("<HH", frame, 6)
    if found != (width, height):
        raise _plain_webp_error(
            f"its dimensions are {found[0] & 0x3FFF}x{found[1] & 0x3FFF}, not {width}x{height}"
        )
    return data


_FREE_MB = re.compile(
    r"cannot make enough memory available on \S+: need [\d.]+ MB device"
    r"(?: / [\d.]+ MB budget)?, available ([\d.]+) MB device"
)
_NO_GPU = re.compile(
    r"busy or unavailable|no CUDA-capable device|failed to initialize CUDA"
    r"|CUDA driver version is insufficient",
    re.IGNORECASE,
)
_OUT_OF_MEMORY = re.compile(r"out of memory", re.IGNORECASE)
_LOG_TAIL = re.compile(r"\s+---\s+\S+:\d+\s*$")
_PREFIX = {
    "engine_failed": "The image engine reported an error",
    "engine_failed_to_start": "The image engine couldn't start",
}


def _quote(line: str) -> str:
    """An engine log line as a sentence fragment: no `[E]` tag, no `--- file.cpp:123` tail, cut short."""
    text = " ".join(_LOG_TAIL.sub("", line.removeprefix("[E]")).split())
    return text if len(text) <= 200 else text[:199].rstrip() + "…"


def classify(
    last_error: Sequence[object], default: str = "engine_failed"
) -> tuple[str, str]:
    """The error code and message for the engine's last [E]/[W] log lines.

    A GPU that refuses CUDA is told apart from one that is merely short of memory (the warning that
    names how much was free is the only place that figure appears). Warnings never count as the error
    to quote, and the "GPU is busy" signature is only believed in error lines: a start that failed on
    its model files logs it as a warning too. Anything else gets `default` and the first error line.
    """
    lines = [line for line in last_error if isinstance(line, str)]
    errors = [line for line in lines if not line.startswith("[W]")]
    if any(_NO_GPU.search(line) for line in errors):
        return "cuda_unavailable", MESSAGES["cuda_unavailable"]
    for line in lines:
        if found := _FREE_MB.search(line):
            return "vram_busy", MESSAGES["vram_busy"].format(
                free=round(float(found[1]))
            )
    if any(_OUT_OF_MEMORY.search(line) for line in errors):
        return "vram_busy", MESSAGES["vram_busy_unknown"]
    quote = _quote(errors[0]) if errors else ""
    detail = f": {quote}{'' if quote[-1:] in '.…!?' else '.'}" if quote else "."
    return default, _PREFIX[default] + detail


class GpuLease:
    """A held lock on the GPU. Release it; letting go of the last reference releases it too, as a backstop."""

    __slots__ = ("_fd",)

    def __init__(self, fd: int) -> None:
        self._fd: int | None = fd

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            os.close(fd)

    def __del__(self) -> None:
        if self._fd is not None:
            log.warning("GPU lock released by garbage collection")
            self.release()


class GpuLock:
    """One advisory flock on the engine's socket directory, shared by every process that uses the GPU.

    An image takes it exclusively and local Ollama replies take it shared, so a reply never starts while
    an image is being made and an image waits for running replies to end. Every lease has a descriptor
    of its own, so two leases in one process conflict like two processes do. The kernel drops the lock
    when a process dies, however it dies.
    """

    def __init__(self, directory: str | os.PathLike[str]) -> None:
        self._directory = directory

    def _open(self) -> int:
        return os.open(self._directory, os.O_RDONLY | os.O_DIRECTORY)

    def _flock(self, how: int) -> GpuLease | None:
        fd = self._open()
        try:
            fcntl.flock(fd, how | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise
        return GpuLease(fd)

    def share(self) -> GpuLease | None:
        """For a local reply: a lease, or Busy while an image is being made. None where there is no directory
        to lock, because then there is no image engine to make room for either."""
        try:
            lease = self._flock(fcntl.LOCK_SH)
        except OSError:
            return None
        if lease is None:
            raise Busy(MESSAGES["gpu_paused"])
        return lease

    def try_exclusive(self) -> GpuLease | None:
        """For an image: a lease if the GPU is free right now, else None."""
        return self._flock(fcntl.LOCK_EX)

    async def exclusive(
        self, *, wait: float = LOCK_WAIT, tick: float = LOCK_TICK
    ) -> GpuLease:
        """Wait up to `wait` seconds for the GPU, looking again every `tick` seconds."""
        fd = self._open()
        loop = asyncio.get_running_loop()
        give_up = loop.time() + wait
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return GpuLease(fd)
                except BlockingIOError:
                    pass
                if loop.time() >= give_up:
                    raise ImageError(
                        "gpu_busy", MESSAGES["gpu_busy"].format(minutes=wait / 60)
                    )
                await asyncio.sleep(tick)
        except BaseException:
            os.close(fd)
            raise


async def _resident(client: httpx2.AsyncClient, cfg: OllamaConfig) -> list[str] | None:
    """Names of the Ollama models that hold VRAM, or None when Ollama can't say."""
    try:
        reply = await client.get(f"{cfg.url}/api/ps", timeout=3.0)
        models = reply.json().get("models") if reply.status_code == 200 else None
    except (httpx2.HTTPError, ValueError, AttributeError):
        return None
    if not isinstance(models, list):
        return None
    return [
        str(entry.get("model") or entry.get("name"))
        for entry in models
        if isinstance(entry, dict) and (entry.get("size_vram") or 0) > 0
    ]


async def unload_ollama(
    client: httpx2.AsyncClient,
    cfg: OllamaConfig,
    *,
    settle: float = UNLOAD_WAIT,
    tick: float = LOCK_TICK,
) -> None:
    """Make room on the GPU: ask Ollama to unload every model that holds VRAM, and wait until none does.

    Ollama defers an unload while a model is mid-reply, so if one is still there after `settle` seconds
    the image is refused (ollama_busy) rather than started into a GPU that can't hold it. An Ollama that
    is down, or doesn't answer sensibly, is skipped: it has nothing loaded that we could unload.
    """
    resident = await _resident(client, cfg)
    if not resident:
        return
    for name in resident:
        try:
            await client.post(
                f"{cfg.url}/api/generate",
                json={"model": name, "keep_alive": 0},
                timeout=10.0,
            )
        except httpx2.HTTPError:
            pass  # the check below decides whether it mattered
    loop = asyncio.get_running_loop()
    give_up = loop.time() + settle
    while resident := await _resident(client, cfg):
        if loop.time() >= give_up:
            raise ImageError(
                "ollama_busy", MESSAGES["ollama_busy"].format(name=resident[0])
            )
        await asyncio.sleep(tick)


def deadline_for(est_s: float) -> float:
    """Seconds an image may take once it has the GPU: three times its estimate and a minute, at most five minutes."""
    return min(DEADLINE_CAP, 3 * est_s + 60)


@dataclass(frozen=True, slots=True)
class Pacing:
    """How often jobs look at the lock, at Ollama and at the engine, and how long they wait. Tests shrink these."""

    tick: float = LOCK_TICK
    lock_wait: float = LOCK_WAIT
    unload_wait: float = UNLOAD_WAIT


PACING = Pacing()


@dataclass(frozen=True, slots=True)
class ImagePlan:
    """Everything one image needs, decided before the job starts. The seed is always chosen by us."""

    chat_id: str
    model_id: str  # as the catalog names it: "images/zimage"
    native: str  # as the engine names it: "zimage"
    prompt: str
    shape: str
    width: int
    height: int
    seed: int
    steps: int
    sample_method: str | None
    est_s: float  # warm seconds for this shape, which sets the time limit


class Interrupted(Exception):
    """Stop was pressed or the time ran out. `reason` is "stopped" or "timeout"."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class Outcome:
    status: str  # done, stopped or error
    image: bytes | None = None
    error: ImageError | None = None


class Job:
    """One image being made. Anyone may listen to it; nobody has to."""

    def __init__(self, plan: ImagePlan, lease: GpuLease | None, began: float) -> None:
        self.plan = plan
        self.lease = lease  # None while it waits for the GPU
        # When the work started: the GPU was taken (or the job was asked for, if it never was).
        self.began = began
        self.task: asyncio.Task[None] | None = None
        self.final: dict[str, object] | None = None  # the event that ended it
        # Until the engine reports something the job is "waiting", which also covers making room in Ollama.
        self.phase = "waiting"
        self.step: int | None = None
        self.steps: int | None = None
        self.load_ms: int | None = None
        self.engine_job: str | None = None
        # What the engine is doing for this job: "submitting" (maybe starting up), "queued" or "generating".
        self.engine_phase: str | None = None
        # Resolved with "stopped" or "timeout"; every wait in the job races it.
        self.interrupt: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._timer: asyncio.TimerHandle | None = None
        self._last: dict[str, object] | None = None
        self._listeners: list[asyncio.Queue[dict[str, object]]] = []

    def subscribe(self) -> asyncio.Queue[dict[str, object]]:
        """A queue of events from now on, starting with where the job is (or with how it ended)."""
        queue: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        if self.final:
            queue.put_nowait(self.final)
            return queue
        if self._last:
            queue.put_nowait(self._last)
        self._listeners.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, object]]) -> None:
        if queue in self._listeners:
            self._listeners.remove(queue)

    def report(self, progress: Progress) -> None:
        event: dict[str, object] = {"t": "progress", "phase": progress.phase}
        if progress.step is not None:
            event |= {"step": progress.step, "steps": progress.steps}
        if event == self._last:
            return
        self._last = event
        self.phase, self.step, self.steps = (
            progress.phase,
            progress.step,
            progress.steps,
        )
        for queue in self._listeners:
            queue.put_nowait(event)

    def request_stop(self) -> None:
        if not self.interrupt.done():
            self.interrupt.set_result("stopped")

    def arm_deadline(self, seconds: float) -> None:
        self._timer = asyncio.get_running_loop().call_later(seconds, self._expire)

    def _expire(self) -> None:
        if not self.interrupt.done():
            self.interrupt.set_result("timeout")

    def end(self, event: dict[str, object]) -> None:
        if self._timer:
            self._timer.cancel()
        self.final = event
        for queue in self._listeners:
            queue.put_nowait(event)
        self._listeners.clear()

    def pending_json(self) -> dict[str, object]:
        return {
            "model": self.plan.model_id,
            "shape": self.plan.shape,
            "phase": self.phase,
            "step": self.step,
            "steps": self.steps,
        }


# The loop only keeps weak references to tasks. A job must not end because nothing points at it.
_TASKS: set[asyncio.Task[None]] = set()


class ImageJobs:
    """Image jobs by chat. A job is a task of its own: the request that started it can end, or be cancelled
    by the server when the browser goes away, without touching it. Its listeners only watch.
    """

    def __init__(
        self,
        store: Store,
        engine: EngineClient,
        gpu: GpuLock,
        client: httpx2.AsyncClient,
        ollama: OllamaConfig,
        *,
        pacing: Pacing = PACING,
        deadline_for: Callable[[float], float] = deadline_for,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._engine = engine
        self.gpu = gpu
        self._client = client
        self._ollama = ollama
        self._pacing = pacing
        self._deadline_for = deadline_for
        self._monotonic = monotonic
        self._jobs: dict[str, Job] = {}

    @property
    def waiting(self) -> int:
        """Jobs that are queued for the GPU, not yet holding it."""
        return sum(1 for job in self._jobs.values() if job.lease is None)

    def admit(self, chat_id: str | None) -> None:
        """Raise Busy unless a job for this chat could start: one image per chat, one more may wait."""
        if chat_id in self._jobs:
            raise Busy("An image is already being made in this chat.")
        if self.waiting:
            raise Busy("Another image is already waiting.")

    def start(self, plan: ImagePlan) -> Job:
        """Begin a job. It takes the GPU now if it is free, so what the next request sees is exact."""
        self.admit(plan.chat_id)
        try:
            lease = self.gpu.try_exclusive()
        except OSError:
            lease = None  # the job will try again and fail with the reason
        job = Job(plan, lease, self._monotonic())
        if lease is None:
            job.report(Progress("waiting"))
        self._jobs[plan.chat_id] = job
        job.task = asyncio.create_task(self._run(job), name=f"image-{plan.chat_id}")
        _TASKS.add(job.task)
        job.task.add_done_callback(_TASKS.discard)
        return job

    def stop(self, chat_id: str) -> bool:
        """Ask the chat's job to stop. False when there is none."""
        job = self._jobs.get(chat_id)
        if job is None:
            return False
        job.request_stop()
        return True

    def pending(self, chat_id: str) -> dict[str, object] | None:
        job = self._jobs.get(chat_id)
        return job.pending_json() if job else None

    async def shutdown(self) -> None:
        """Stop every job without touching the engine, which finishes alone and idles out."""
        jobs = list(self._jobs.values())
        tasks = [job.task for job in jobs if job.task]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for job in jobs:  # one cancelled before its first step never ran its own ending
            self._finish(job, Outcome("stopped"))

    # the job

    async def _run(self, job: Job) -> None:
        outcome = None
        try:
            try:
                outcome = Outcome("done", await self._work(job))
            except Interrupted as stop:
                outcome = await self._interrupted(job, stop.reason)
            except ImageError as err:
                outcome = Outcome("error", error=err)
            except EngineError as err:
                outcome = Outcome("error", error=self._explain_engine_error(err, job))
            except Exception:
                log.exception("image job failed in chat %s", job.plan.chat_id)
                outcome = Outcome(
                    "error", error=ImageError("internal", MESSAGES["internal"])
                )
        finally:
            # Also reached when the server cancels the job: it is then recorded as stopped, and awaits nothing.
            self._finish(job, outcome or Outcome("stopped"))

    async def _guard[T](self, job: Job, awaitable: Awaitable[T]) -> T:
        """Await `awaitable` unless Stop or the deadline comes first, which raises Interrupted."""
        if job.interrupt.done():
            if inspect.iscoroutine(awaitable):
                awaitable.close()
            raise Interrupted(job.interrupt.result())
        work = asyncio.ensure_future(awaitable)
        try:
            await asyncio.wait(
                {work, job.interrupt}, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            work.cancel()
            raise
        if work.done():
            return work.result()
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)
        raise Interrupted(job.interrupt.result())

    async def _take_the_gpu(self, job: Job) -> None:
        # Assigned in the same step that gets the lock, so a Stop at that instant cannot lose the lease.
        job.lease = await self.gpu.exclusive(
            wait=self._pacing.lock_wait, tick=self._pacing.tick
        )

    async def _work(self, job: Job) -> bytes:
        plan, pace = job.plan, self._pacing
        if job.lease is None:
            await self._guard(job, self._take_the_gpu(job))
            job.began = self._monotonic()
        job.arm_deadline(self._deadline_for(plan.est_s))
        await self._guard(
            job,
            unload_ollama(
                self._client, self._ollama, settle=pace.unload_wait, tick=pace.tick
            ),
        )
        status = await self._guard(job, self._engine.status())
        cold = not (status.state == "ready" and status.engine == plan.native)
        if cold:
            job.report(Progress("starting"))
        request = ImageRequest(
            plan.native,
            plan.prompt,
            plan.width,
            plan.height,
            plan.seed,
            plan.steps,
            plan.sample_method,
        )
        job.engine_phase = "submitting"
        asked = self._monotonic()
        job.engine_job = await self._guard(job, self._engine.submit(request))
        if cold:
            job.load_ms = round((self._monotonic() - asked) * 1000)
        while True:
            state = await self._guard(
                job, self._engine.poll(plan.native, job.engine_job)
            )
            if state.status == "completed":
                job.engine_phase = None
                return self._image(state.image_b64, plan)
            if state.status == "failed":
                raise await self._explain_failure(job, state.error_message)
            if state.status == "cancelled":
                raise ImageError(
                    "engine_failed", "The image engine cancelled the image."
                )
            if state.status == "queued":
                job.engine_phase = "queued"
                job.report(Progress("queued"))
            else:
                job.engine_phase = "generating"
                steps = state.total_steps or plan.steps
                if state.step is None:
                    job.report(Progress("sampling"))
                elif state.step >= steps:
                    job.report(Progress("finishing"))
                else:
                    job.report(Progress("sampling", state.step, steps))
            await self._guard(job, asyncio.sleep(pace.tick))

    @staticmethod
    def _image(b64: str | None, plan: ImagePlan) -> bytes:
        """The engine's image, decoded strictly and checked. Nothing else is ever stored."""
        text = b64 or ""
        if len(text) > MAX_IMAGE_BYTES * 4 // 3 + 8:
            raise _plain_webp_error("it is larger than 8 MiB")
        try:
            data = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as err:
            raise _plain_webp_error("it is not valid base64") from err
        return validate_webp(data, plan.width, plan.height)

    async def _explain_failure(
        self, job: Job, engine_message: str | None
    ) -> ImageError:
        """The engine says the job failed; its log, fetched now, says why."""
        lines: tuple[str, ...] = ()
        try:
            lines = (await self._guard(job, self._engine.status())).last_error
        except EngineError:
            pass
        if not lines and engine_message:
            lines = (f"[E] {engine_message}",)
        return ImageError(*classify(lines))

    @staticmethod
    def _explain_engine_error(err: EngineError, job: Job) -> ImageError:
        match err.code:
            case "engine_unreachable":
                return ImageError(err.code, MESSAGES["engine_unreachable"])
            case "engine_failed_to_start":
                return ImageError(
                    *classify(err.lines, default="engine_failed_to_start")
                )
            case "engine_gone" | "engine_io":
                return ImageError("engine_crashed", MESSAGES["engine_crashed"])
            case "engine_timeout" if job.engine_phase in ("queued", "generating"):
                return ImageError("engine_crashed", MESSAGES["engine_crashed"])
            case "engine_timeout":
                return ImageError(
                    "engine_failed", "The image engine did not answer in time."
                )
            case "bad_request":
                return ImageError(
                    "bad_request",
                    f"The image engine rejected the request: {err.message}".rstrip(
                        ": "
                    ),
                )
            case "unknown_model":
                return ImageError(
                    "engine_failed", "The image engine does not have that model."
                )
            case _:
                return ImageError(
                    "engine_failed", err.message or MESSAGES["engine_failed"]
                )

    async def _interrupted(self, job: Job, reason: str) -> Outcome:
        """Stop or the deadline: do not leave the engine working on an image nobody wants."""
        plan = job.plan
        needs_kill = job.engine_phase is not None
        if reason == "stopped" and job.engine_phase == "queued" and job.engine_job:
            try:
                needs_kill = not await self._engine.cancel(plan.native, job.engine_job)
            except EngineError:
                pass
        if needs_kill:
            try:
                await self._engine.kill()
            except EngineError as err:
                log.warning("could not kill the image engine: %s", err.code)
        if reason == "timeout":
            return Outcome("error", error=ImageError("timeout", MESSAGES["timeout"]))
        return Outcome("stopped")

    def _finish(self, job: Job, outcome: Outcome) -> None:
        """Store the reply, tell the listeners, free the GPU and forget the job. Runs once, and never awaits."""
        if job.final is not None:
            return
        try:
            message = self._save(job, outcome)
        except Exception:
            log.exception("could not store the image for chat %s", job.plan.chat_id)
            internal = {"code": "internal", "message": MESSAGES["internal"]}
            message = self._unsaved(job, "error", internal, self._usage(job))
        try:
            job.end(
                {
                    "t": "error" if message.error else "done",
                    "message": message.to_json(),
                }
            )
        finally:
            if job.lease:
                job.lease.release()
            if self._jobs.get(job.plan.chat_id) is job:
                del self._jobs[job.plan.chat_id]

    def _usage(self, job: Job) -> dict[str, object]:
        usage: dict[str, object] = {
            "total_ms": round((self._monotonic() - job.began) * 1000)
        }
        if job.load_ms is not None:
            usage["load_ms"] = job.load_ms
        return usage | {"steps": job.plan.steps, "shape": job.plan.shape}

    def _unsaved(
        self,
        job: Job,
        status: str,
        error: dict[str, str] | None,
        usage: dict[str, object],
    ) -> Message:
        """The reply as the listeners get it when it could not be stored: it has no id."""
        plan = job.plan
        return Message(
            "",
            plan.chat_id,
            0,
            "assistant",
            "",
            None,
            plan.model_id,
            None,
            usage,
            status,
            error,
            0,
        )

    def _save(self, job: Job, outcome: Outcome) -> Message:
        plan = job.plan
        status = "error" if outcome.error else outcome.status
        error = (
            {"code": outcome.error.code, "message": outcome.error.message}
            if outcome.error
            else None
        )
        usage = self._usage(job)
        images = []
        if outcome.image is not None:
            images = [
                ImageRow(
                    plan.width,
                    plan.height,
                    plan.seed,
                    plan.steps,
                    plan.model_id,
                    outcome.image,
                )
            ]
        try:
            message = self._store.add_message(
                plan.chat_id,
                "assistant",
                "",
                model=plan.model_id,
                usage=usage,
                status=status,
                error=error,
                images=images,
            )
        except sqlite3.IntegrityError:
            # The chat was deleted while the image was made; there is nowhere to keep it.
            log.info("chat %s vanished during its image", plan.chat_id)
            message = self._unsaved(job, status, error, usage)
        log.info(
            "image chat=%s model=%s status=%s code=%s %s",
            plan.chat_id,
            plan.model_id,
            status,
            (error or {}).get("code"),
            " ".join(f"{k}={v}" for k, v in usage.items()),
        )
        return message
