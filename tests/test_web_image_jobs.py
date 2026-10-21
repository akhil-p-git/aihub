"""Image jobs: server-owned tasks that make one image each, whoever is or is not still listening."""

import asyncio
import base64
import contextlib
import os
import re
import sqlite3
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx2
import pytest
from aihub_web.config import OllamaConfig
from aihub_web.db import Store
from aihub_web.images import (
    Busy,
    GpuLock,
    ImageJobs,
    ImagePlan,
    Job,
    Pacing,
    deadline_for,
)
from aihub_web.providers.images import EngineClient
from helpers import (
    FAST_PACING as FAST,
)
from helpers import (
    HOLD,
    Clock,
    FakeEngine,
    FakeOllama,
    completed,
    engine_errors,
    failed,
    finished,
    generating,
    handler_for,
    holding,
    queued,
    webp,
)


def plan_for(chat_id: str, **fields) -> ImagePlan:
    return ImagePlan(
        **{
            "chat_id": chat_id,
            "model_id": "images/zimage",
            "native": "zimage",
            "prompt": "a red fox in snow",
            "shape": "wide",
            "width": 1024,
            "height": 576,
            "seed": 42,
            "steps": 4,
            "sample_method": None,
            "est_s": 12.0,
            **fields,
        }
    )


@dataclass
class Rig:
    jobs: ImageJobs
    store: Store
    engine: FakeEngine
    ollama: FakeOllama
    lock: GpuLock
    timeline: list
    directory: Path

    def chat(self, prompt: str = "a red fox in snow") -> str:
        chat = self.store.create_chat(prompt, "images/zimage", None)
        self.store.add_message(chat.id, "user", prompt)
        return chat.id

    def replies(self, chat_id: str) -> list:
        return [m for m in self.store.messages(chat_id) if m.role == "assistant"]


@contextlib.asynccontextmanager
async def rig(
    tmp_path: Path,
    socket_dir: Path,
    *,
    engine: FakeEngine | None = None,
    ollama: FakeOllama | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
    pacing: Pacing = FAST,
    deadline: Callable[[float], float] = lambda est_s: 30.0,
    monotonic: Callable[[], float] = time.monotonic,
    lock_directory: Path | None = None,
) -> AsyncIterator[Rig]:
    timeline: list = []
    engine = engine or FakeEngine()
    ollama = ollama or FakeOllama()
    engine.timeline = ollama.timeline = timeline
    store = Store(tmp_path / "chats.db")
    client = httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler_for(ollama=ollama))
    )
    lock = GpuLock(lock_directory or socket_dir)
    jobs = ImageJobs(
        store,
        EngineClient("/never/used.sock", transport=transport or engine.transport()),
        lock,
        client,
        OllamaConfig(),
        pacing=pacing,
        deadline_for=deadline,
        monotonic=monotonic,
    )
    try:
        yield Rig(jobs, store, engine, ollama, lock, timeline, socket_dir)
    finally:
        await jobs.shutdown()
        await client.aclose()
        store.close()


async def collect(job: Job, timeout: float = 10.0) -> list[dict]:
    """Everything the job publishes, up to and including its last event."""
    queue = job.subscribe()
    events = []
    while True:
        event = await asyncio.wait_for(queue.get(), timeout)
        events.append(event)
        if event["t"] != "progress":
            return events


async def until(check: Callable[[], object], timeout: float = 5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition never became true")


def phase_of(job: Job, phase: str) -> Callable[[], bool]:
    return lambda: job.phase == phase


def progress(*phases: str) -> list[dict]:
    return [{"t": "progress", "phase": phase} for phase in phases]


def open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


@pytest.mark.anyio
class TestMakingAnImage:
    async def test_the_job_reports_its_progress_and_saves_the_image(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            chat_id = r.chat()
            events = await collect(r.jobs.start(plan_for(chat_id)))
            *steps, last = events
            assert steps == [
                *progress("queued"),
                {"t": "progress", "phase": "sampling", "step": 1, "steps": 4},
                {"t": "progress", "phase": "sampling", "step": 2, "steps": 4},
                {"t": "progress", "phase": "sampling", "step": 3, "steps": 4},
                *progress("finishing"),
            ]
            assert last["t"] == "done"
            message = last["message"]
            [stored] = r.replies(chat_id)
            assert message == stored.to_json()
            assert (stored.status, stored.content, stored.error) == ("done", "", None)
            assert (stored.model, stored.effort) == ("images/zimage", None)
            [image] = stored.images
            assert re.fullmatch(r"[0-9a-f]{16}", image["id"])
            assert {k: v for k, v in image.items() if k != "id"} == {
                "w": 1024,
                "h": 576,
                "seed": 42,
                "steps": 4,
                "model": "images/zimage",
                "ref": None,
            }
            assert r.store.get_image(chat_id, image["id"]) == (
                "image/webp",
                webp("wide"),
            )
            assert set(stored.usage) == {"total_ms", "steps", "shape"}
            assert (stored.usage["steps"], stored.usage["shape"]) == (4, "wide")

    async def test_the_engine_is_asked_for_exactly_what_the_plan_says(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            plan = plan_for(
                r.chat(),
                prompt="a lighthouse",
                seed=7,
                steps=8,
                shape="tall",
                width=576,
                height=1024,
                native="klein4b",
                sample_method="euler",
            )
            r.engine.engine = "klein4b"
            await collect(r.jobs.start(plan))
            [body] = r.engine.submitted
            assert (body["prompt"], body["seed"], body["width"], body["height"]) == (
                "a lighthouse",
                7,
                576,
                1024,
            )
            assert body["sample_params"]["sample_steps"] == 8
            assert body["sample_params"]["sample_method"] == "euler"
            assert body["embed_image_metadata"] is False
            assert ("POST", "/e/klein4b/sdcpp/v1/img_gen") in r.engine.calls

    async def test_unloading_ollama_comes_before_the_engine_is_asked_anything(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir, ollama=FakeOllama("qwen3:8b")) as r:
            await collect(r.jobs.start(plan_for(r.chat())))
            who = [who for who, *_ in r.timeline]
            assert who[0] == "ollama" and "engine" in who
            assert who.index("engine") > max(
                i for i, w in enumerate(who) if w == "ollama"
            )
            assert r.ollama.resident == []
            assert r.ollama.posts() == [{"model": "qwen3:8b", "keep_alive": 0}]

    async def test_a_warm_engine_is_not_announced_and_a_cold_one_is(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            chat_id = r.chat()
            warm = await collect(r.jobs.start(plan_for(chat_id)))
            assert {"t": "progress", "phase": "starting"} not in warm
            assert "load_ms" not in r.replies(chat_id)[0].usage
        cold_engine = FakeEngine(model=None)
        cold_engine.submit_delay = 0.2
        async with rig(tmp_path, socket_dir, engine=cold_engine) as r:
            chat_id = r.chat()
            cold = await collect(r.jobs.start(plan_for(chat_id)))
            assert cold[0] == {"t": "progress", "phase": "starting"}
            assert r.replies(chat_id)[0].usage["load_ms"] >= 150

    async def test_an_engine_with_another_model_loaded_has_to_start_this_one(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir, engine=FakeEngine(model="klein4b")) as r:
            events = await collect(r.jobs.start(plan_for(r.chat())))
            assert events[0] == {"t": "progress", "phase": "starting"}

    async def test_a_progress_report_without_a_step_is_an_indeterminate_bar(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([queued(1), generating(), generating(), finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            events = await collect(r.jobs.start(plan_for(r.chat())))
            assert events[:-1] == progress(
                "queued", "sampling"
            )  # said once, not per poll

    async def test_the_step_count_is_the_engines_when_it_differs_from_the_plans(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 6), generating(6, 6), finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            events = await collect(r.jobs.start(plan_for(r.chat(), steps=4)))
            assert events[0] == {
                "t": "progress",
                "phase": "sampling",
                "step": 1,
                "steps": 6,
            }
            assert events[1] == {"t": "progress", "phase": "finishing"}

    async def test_the_gpu_and_the_registry_are_free_afterwards(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        before = open_fds()
        async with rig(tmp_path, socket_dir) as r:
            chat_id = r.chat()
            for _ in range(3):
                await collect(r.jobs.start(plan_for(chat_id)))
            assert r.jobs.pending(chat_id) is None
            lease = r.lock.try_exclusive()
            assert lease is not None
            lease.release()
        assert (
            open_fds() <= before + 1
        )  # the database connection of the next rig, at most

    async def test_pending_says_where_a_running_job_is(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(2, 4), HOLD, finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(phase_of(job, "sampling"))
            assert r.jobs.pending(chat_id) == {
                "model": "images/zimage",
                "shape": "wide",
                "phase": "sampling",
                "step": 2,
                "steps": 4,
            }
            assert r.jobs.pending("0123456789abcdef") is None
            engine.release()
            await collect(job)
            assert r.jobs.pending(chat_id) is None

    async def test_time_spent_waiting_for_the_gpu_is_not_time_spent_making_the_image(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        clock = Clock(1000.0)
        async with rig(tmp_path, socket_dir, monotonic=clock) as r:
            held = r.lock.try_exclusive()
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await asyncio.sleep(0.05)
            clock.advance(100)  # a hundred seconds go by while it waits
            held.release()
            await collect(job)
            assert r.replies(chat_id)[0].usage["total_ms"] < 1000

    async def test_the_gpu_is_held_for_the_whole_job_even_when_it_had_to_wait_for_it(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4), HOLD, finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            held = r.lock.try_exclusive()
            job = r.jobs.start(plan_for(r.chat()))
            await asyncio.sleep(0.05)
            held.release()
            await until(phase_of(job, "sampling"))
            assert r.lock.try_exclusive() is None
            with pytest.raises(Busy):
                r.lock.share()
            engine.release()
            await collect(job)
            assert gpu_is_free(r)

    async def test_a_stop_before_the_job_has_begun_leaves_ollama_and_the_engine_alone(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir, ollama=FakeOllama("qwen3:8b")) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            assert r.jobs.stop(chat_id) is True  # before the task has had a turn
            events = await collect(job)
            assert events[-1]["message"]["status"] == "stopped"
            assert r.ollama.calls == [] and r.engine.calls == []
            assert gpu_is_free(r)


@pytest.mark.anyio
class TestWhenItGoesWrong:
    async def run_to_the_end(self, r: Rig, **plan) -> dict:
        chat_id = r.chat()
        events = await collect(r.jobs.start(plan_for(chat_id, **plan)))
        assert events[-1]["t"] == "error", events
        message = events[-1]["message"]
        [stored] = r.replies(chat_id)
        assert message == stored.to_json()
        assert (stored.status, stored.content, stored.images) == ("error", "", None)
        assert r.lock.try_exclusive() is not None and r.jobs.pending(chat_id) is None
        return message["error"]

    async def test_a_gpu_that_will_not_answer(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine(model=None)
        lines = engine_errors("gpu_busy_or_unavailable")
        engine.submit_response = (
            503,
            {"code": "engine_failed_to_start", "last_error": lines},
        )
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            assert await self.run_to_the_end(r) == {
                "code": "cuda_unavailable",
                "message": "The GPU didn't respond. If this keeps happening, restart the PC.",
            }

    async def test_an_engine_that_cannot_start_names_the_first_error(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine(model=None)
        lines = engine_errors("model_files_wrong")
        engine.submit_response = (
            503,
            {"code": "engine_failed_to_start", "last_error": lines},
        )
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            error = await self.run_to_the_end(r)
            assert error["code"] == "engine_failed_to_start"
            assert error["message"].startswith(
                "The image engine couldn't start: Conditioner model tensor"
            )

    async def test_an_engine_that_is_not_running_says_how_to_start_it(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        def refuse(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ConnectError("refused", request=request)

        async with rig(
            tmp_path, socket_dir, transport=httpx2.MockTransport(refuse)
        ) as r:
            assert await self.run_to_the_end(r) == {
                "code": "engine_unreachable",
                "message": (
                    "The image engine isn't running. Run systemctl --user reset-failed "
                    "aihub-images.socket aihub-images.service, then systemctl --user "
                    "start aihub-images.socket."
                ),
            }

    async def test_a_job_that_fails_for_lack_of_memory_says_how_much_there_was(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([queued(1), generating(), failed()])
        engine.last_error = engine_errors("vram_short")
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            error = await self.run_to_the_end(r)
            assert error["code"] == "vram_busy"
            assert error["message"].startswith(
                "The GPU is short of memory (950 MB free)."
            )
            # the engine's log was fetched after the failure, not before
            assert engine.count("GET", "/launcher/status") == 2

    async def test_any_other_engine_failure_quotes_the_engines_own_words(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append(
            [failed("generation_failed", "generate_image returned no results")]
        )
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            error = await self.run_to_the_end(r)
            assert error["code"] == "engine_failed"
            assert "generate_image returned no results" in error["message"]

    async def test_a_job_the_engine_lost_means_it_crashed(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append(
            [
                generating(1, 4),
                (
                    410,
                    {
                        "code": "engine_gone",
                        "last_error": engine_errors("vae_out_of_memory_abort"),
                    },
                ),
            ]
        )
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            assert await self.run_to_the_end(r) == {
                "code": "engine_crashed",
                "message": "The image engine stopped unexpectedly. Retry starts it again.",
            }

    @pytest.mark.parametrize("status", [502, 504])
    async def test_a_launcher_that_cannot_get_an_answer_from_the_engine_means_a_crash(
        self, tmp_path: Path, socket_dir: Path, status: int
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append(
            [
                generating(1, 4),
                (status, {"code": "engine_unreachable", "last_error": []}),
            ]
        )
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            assert (await self.run_to_the_end(r))["code"] == "engine_crashed"

    async def test_a_broken_connection_while_polling_is_a_crash_too(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        inner = engine.transport()
        polls = 0

        async def flaky(request: httpx2.Request) -> httpx2.Response:
            nonlocal polls
            if "/jobs/" in request.url.path:
                polls += 1
                if polls == 3:
                    raise httpx2.ReadError("reset", request=request)
            return await inner.handle_async_request(request)

        async with rig(
            tmp_path, socket_dir, engine=engine, transport=httpx2.MockTransport(flaky)
        ) as r:
            assert (await self.run_to_the_end(r))["code"] == "engine_crashed"

    async def test_a_socket_directory_that_has_vanished_means_no_engine(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(
            tmp_path, socket_dir, lock_directory=socket_dir / "vanished"
        ) as r:
            chat_id = r.chat()
            events = await collect(r.jobs.start(plan_for(chat_id)))
            error = events[-1]["message"]["error"]
            assert error["code"] == "engine_unreachable"
            assert error["message"].startswith("The image engine isn't running.")
            assert r.replies(chat_id)[0].status == "error"
            assert r.engine.calls == [] and r.jobs.pending(chat_id) is None

    async def test_a_cancel_nobody_asked_for_is_a_failure(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append(
            [
                {
                    "status": "cancelled",
                    "queue_position": 0,
                    "result": None,
                    "error": {
                        "code": "cancelled",
                        "message": "job cancelled by client",
                    },
                }
            ]
        )
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            assert (await self.run_to_the_end(r))["code"] == "engine_failed"

    @pytest.mark.parametrize(
        ("status", "reply", "code"),
        [
            (400, {"error": "invalid generation parameters"}, "bad_request"),
            (404, {"code": "unknown_model"}, "engine_failed"),
            (429, {"error": "job queue is full"}, "engine_failed"),
            (500, {"error": "boom"}, "engine_failed"),
        ],
    )
    async def test_the_engine_refusing_the_request(
        self, tmp_path: Path, socket_dir: Path, status: int, reply: object, code: str
    ) -> None:
        engine = FakeEngine()
        engine.submit_response = (status, reply)
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            assert (await self.run_to_the_end(r))["code"] == code

    async def test_a_bug_in_the_job_is_an_error_that_says_so_without_the_details(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        def explode(request: httpx2.Request) -> httpx2.Response:
            raise RuntimeError("boom: /home/akhil/secret/path")

        async with rig(
            tmp_path, socket_dir, transport=httpx2.MockTransport(explode)
        ) as r:
            error = await self.run_to_the_end(r)
            assert error["code"] == "internal"
            assert "boom" not in error["message"] and "secret" not in error["message"]

    async def test_a_database_that_cannot_store_the_reply_still_ends_the_job(
        self, tmp_path: Path, socket_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:

            def full(*args, **kwargs):
                raise sqlite3.OperationalError("database or disk is full")

            chat_id = r.chat()
            monkeypatch.setattr(r.store, "add_message", full)
            events = await collect(r.jobs.start(plan_for(chat_id)))
            assert events[-1]["t"] == "error"
            assert events[-1]["message"]["error"]["code"] == "internal"
            assert events[-1]["message"]["id"] == ""
            assert gpu_is_free(r)

    async def test_a_model_that_will_not_leave_ollama_stops_the_job_before_the_engine(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        ollama = FakeOllama("qwen3:8b", stubborn=("qwen3:8b",))
        async with rig(tmp_path, socket_dir, ollama=ollama) as r:
            error = await self.run_to_the_end(r)
            assert error["code"] == "ollama_busy" and "qwen3:8b" in error["message"]
            assert r.engine.calls == []

    async def test_an_ollama_that_answers_nonsense_is_skipped(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            r.ollama.resident = "not even a list"
            events = await collect(r.jobs.start(plan_for(r.chat())))
            assert events[-1]["t"] == "done"


@pytest.mark.anyio
class TestWhatTheEngineReturns:
    async def refused(self, tmp_path: Path, socket_dir: Path, reply: dict) -> dict:
        engine = FakeEngine()
        engine.scripts.append([reply])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            events = await collect(r.jobs.start(plan_for(chat_id)))
            assert events[-1]["t"] == "error"
            assert r.store.image_bytes_total() == 0
            return events[-1]["message"]["error"]

    async def test_a_file_with_metadata_is_not_stored(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        real = (
            Path(__file__).parent
            / "fixtures"
            / "images"
            / "lab-engine-output-with-xmp.webp"
        ).read_bytes()
        error = await self.refused(tmp_path, socket_dir, completed(real))
        assert error["code"] == "bad_output"

    async def test_an_image_of_the_wrong_size_is_not_stored(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        error = await self.refused(tmp_path, socket_dir, completed(webp("square")))
        assert error["code"] == "bad_output" and "dimensions" in error["message"]

    @pytest.mark.parametrize(
        "b64",
        [
            "not base64 at all!",
            base64.b64encode(webp("wide")).decode() + "\n",
            base64.b64encode(webp("wide")).decode()[:-2],
            "A" * (12 * 1024 * 1024),
            "",
        ],
    )
    async def test_something_that_does_not_decode_strictly_is_not_stored(
        self, tmp_path: Path, socket_dir: Path, b64: str
    ) -> None:
        reply = completed(b"")
        reply["result"]["images"][0]["b64_json"] = b64
        assert (await self.refused(tmp_path, socket_dir, reply))["code"] == "bad_output"

    async def test_garbage_that_decodes_is_not_stored(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        error = await self.refused(
            tmp_path, socket_dir, completed(b"\x89PNG not a webp" * 9)
        )
        assert error["code"] == "bad_output"


def gpu_is_free(r: Rig) -> bool:
    lease = r.lock.try_exclusive()
    if lease:
        lease.release()
    return lease is not None


@pytest.mark.anyio
class TestStop:
    async def stopped(
        self, r: Rig, job: Job, chat_id: str, *, gpu_free: bool = True
    ) -> list[dict]:
        assert r.jobs.stop(chat_id) is True
        events = await collect(job)
        assert events[-1]["t"] == "done"
        message = events[-1]["message"]
        assert (message["status"], message["images"], message["error"]) == (
            "stopped",
            None,
            None,
        )
        [stored] = r.replies(chat_id)
        assert (stored.status, stored.content, stored.images) == ("stopped", "", None)
        assert r.store.image_bytes_total() == 0
        assert r.jobs.pending(chat_id) is None
        assert gpu_is_free(r) == gpu_free
        return events

    async def test_while_waiting_for_the_gpu(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            held = r.lock.try_exclusive()
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await asyncio.sleep(0.05)
            await self.stopped(r, job, chat_id, gpu_free=False)  # held by someone else
            assert r.engine.calls == [] and r.ollama.calls == []
            held.release()
            assert gpu_is_free(r)

    async def test_while_ollama_is_being_unloaded(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        ollama = FakeOllama("qwen3:8b", stubborn=("qwen3:8b",))
        patient = Pacing(tick=0.01, lock_wait=2.0, unload_wait=30.0)
        async with rig(tmp_path, socket_dir, ollama=ollama, pacing=patient) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(
                lambda: ollama.posts()
            )  # it has asked the model to leave and is waiting
            await self.stopped(r, job, chat_id)
            assert r.engine.calls == []

    async def test_while_the_engine_is_starting(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine(model=None)
        engine.submit_delay = 30
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(phase_of(job, "starting"))
            await asyncio.sleep(0.05)
            events = await self.stopped(r, job, chat_id)
            assert {"t": "progress", "phase": "starting"} in events
            # there is no job id to cancel, so the engine that was starting is killed
            assert (engine.kills, engine.cancels, engine.submitted) == (1, [], [])

    async def test_while_queued_the_job_is_cancelled_and_the_engine_left_alone(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([queued(2)])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(phase_of(job, "queued"))
            await self.stopped(r, job, chat_id)
            assert engine.cancels == ["job_00000001"] and engine.kills == 0

    async def test_a_queued_job_that_cannot_be_cancelled_any_more_is_killed(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([queued(1)])
        engine.cancel_response = (409, {"error": "job is currently generating"})
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(phase_of(job, "queued"))
            await self.stopped(r, job, chat_id)
            assert (len(engine.cancels), engine.kills) == (1, 1)

    async def test_while_generating_the_engine_is_killed(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4)])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(phase_of(job, "sampling"))
            await self.stopped(r, job, chat_id)
            assert engine.kills == 1 and engine.cancels == []

    async def test_a_kill_that_does_not_work_still_ends_the_job_as_stopped(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4)])
        inner = engine.transport()

        async def no_kill(request: httpx2.Request) -> httpx2.Response:
            if request.url.path == "/launcher/kill":
                raise httpx2.ConnectError("refused", request=request)
            return await inner.handle_async_request(request)

        async with rig(
            tmp_path, socket_dir, engine=engine, transport=httpx2.MockTransport(no_kill)
        ) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(phase_of(job, "sampling"))
            await self.stopped(r, job, chat_id)

    async def test_stopping_twice_or_after_the_end_is_harmless(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4)])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(phase_of(job, "sampling"))
            assert r.jobs.stop(chat_id) is True
            r.jobs.stop(chat_id)
            await collect(job)
            assert r.jobs.stop(chat_id) is False
            assert len(r.replies(chat_id)) == 1 and engine.kills == 1

    async def test_there_is_nothing_to_stop_in_a_chat_with_no_job(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            assert r.jobs.stop("0123456789abcdef") is False
            assert r.jobs.stop(r.chat()) is False


@pytest.mark.anyio
class TestDeadline:
    def test_a_job_gets_three_times_its_estimate_and_a_minute_but_never_more_than_five(
        self,
    ) -> None:
        assert deadline_for(23) == 129
        assert deadline_for(12) == 96
        assert deadline_for(5) == 75
        assert deadline_for(80) == 300
        assert deadline_for(1000) == 300

    async def test_a_job_that_runs_out_of_time_is_killed_and_saved_as_a_timeout(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4)])
        async with rig(
            tmp_path, socket_dir, engine=engine, deadline=lambda est: 0.3
        ) as r:
            chat_id = r.chat()
            started = time.monotonic()
            events = await collect(r.jobs.start(plan_for(chat_id)))
            assert 0.25 < time.monotonic() - started < 3
            assert events[-1]["t"] == "error"
            assert events[-1]["message"]["error"]["code"] == "timeout"
            [stored] = r.replies(chat_id)
            assert (stored.status, stored.images) == ("error", None)
            assert (engine.kills, engine.cancels) == (1, [])
            assert r.jobs.pending(chat_id) is None and gpu_is_free(r)

    async def test_the_deadline_is_taken_from_the_estimate_of_the_shape(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        asked = []

        def deadline(est_s: float) -> float:
            asked.append(est_s)
            return 30.0

        async with rig(tmp_path, socket_dir, deadline=deadline) as r:
            await collect(r.jobs.start(plan_for(r.chat(), est_s=12.0)))
        assert asked == [12.0]

    async def test_a_queued_job_that_runs_out_of_time_is_killed_not_cancelled(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([queued(3)])
        async with rig(
            tmp_path, socket_dir, engine=engine, deadline=lambda est: 0.2
        ) as r:
            events = await collect(r.jobs.start(plan_for(r.chat())))
            assert events[-1]["message"]["error"]["code"] == "timeout"
            assert (engine.kills, engine.cancels) == (1, [])

    async def test_the_clock_starts_when_the_job_has_the_gpu_not_when_it_asked(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir, deadline=lambda est: 0.6) as r:
            held = r.lock.try_exclusive()
            job = r.jobs.start(plan_for(r.chat()))
            await asyncio.sleep(0.8)  # longer than the whole deadline, spent waiting
            held.release()
            events = await collect(job)
            assert events[-1]["t"] == "done"

    async def test_time_spent_before_the_engine_is_asked_counts(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        ollama = FakeOllama("qwen3:8b", stubborn=("qwen3:8b",))
        patient = Pacing(tick=0.01, lock_wait=2.0, unload_wait=30.0)
        async with rig(
            tmp_path,
            socket_dir,
            ollama=ollama,
            pacing=patient,
            deadline=lambda est: 0.3,
        ) as r:
            events = await collect(r.jobs.start(plan_for(r.chat())))
            assert events[-1]["message"]["error"]["code"] == "timeout"
            assert (
                r.engine.calls == []
            )  # nothing was started, so there is nothing to kill


@pytest.mark.anyio
class TestListeners:
    async def test_a_listener_that_leaves_does_not_stop_the_image(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4), HOLD, finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            queue = job.subscribe()
            assert (await asyncio.wait_for(queue.get(), 5))["phase"] == "sampling"
            job.unsubscribe(
                queue
            )  # what the stream's finally does when the browser goes away
            engine.release()
            await until(lambda: r.replies(chat_id))
            [stored] = r.replies(chat_id)
            assert stored.status == "done" and stored.images
            assert engine.kills == 0 and engine.cancels == []

    async def test_cancelling_the_task_that_reads_the_events_does_not_cancel_the_job(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        """Starlette cancels the response task when a client disconnects; the job is a task of its own."""
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4), HOLD, finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            reader = asyncio.create_task(collect(job))
            await until(phase_of(job, "sampling"))
            reader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await reader
            engine.release()
            await until(lambda: r.replies(chat_id))
            assert r.replies(chat_id)[0].status == "done"
            assert engine.kills == 0

    async def test_a_listener_that_arrives_late_still_gets_the_ending(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(lambda: r.replies(chat_id))
            assert r.jobs.pending(chat_id) is None
            late = await collect(job)
            assert [e["t"] for e in late] == ["done"]
            assert late[0]["message"]["id"] == r.replies(chat_id)[0].id

    async def test_a_listener_that_arrives_midway_starts_from_where_the_job_is(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(2, 4), HOLD, finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            job = r.jobs.start(plan_for(r.chat()))
            await until(phase_of(job, "sampling"))
            queue = job.subscribe()
            assert await asyncio.wait_for(queue.get(), 5) == {
                "t": "progress",
                "phase": "sampling",
                "step": 2,
                "steps": 4,
            }
            engine.release()

    async def test_every_listener_gets_the_same_events(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            job = r.jobs.start(plan_for(r.chat()))
            first, second = await asyncio.gather(collect(job), collect(job))
            assert first == second and first[-1]["t"] == "done"

    async def test_a_chat_deleted_while_its_image_is_made_loses_the_image_quietly(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4), HOLD, finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(phase_of(job, "sampling"))
            r.store.delete_chat(chat_id)
            engine.release()
            events = await collect(job)
            assert events[-1]["t"] == "done"
            assert (
                events[-1]["message"]["id"] == ""
                and events[-1]["message"]["images"] is None
            )
            assert r.store.image_bytes_total() == 0 and gpu_is_free(r)


@pytest.mark.anyio
class TestSharingTheGpu:
    async def test_a_second_image_waits_for_the_first_and_a_third_is_turned_away(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4), HOLD, finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            first_chat, second_chat, third_chat = (
                r.chat("one"),
                r.chat("two"),
                r.chat("three"),
            )
            first = r.jobs.start(plan_for(first_chat, prompt="one"))
            await until(phase_of(first, "sampling"))

            second = r.jobs.start(plan_for(second_chat, prompt="two"))
            assert second.phase == "waiting" and r.jobs.waiting == 1
            assert r.jobs.pending(second_chat)["phase"] == "waiting"
            queue = second.subscribe()
            assert await asyncio.wait_for(queue.get(), 5) == {
                "t": "progress",
                "phase": "waiting",
            }
            with pytest.raises(Busy, match="Another image is already waiting"):
                r.jobs.admit(third_chat)
            await asyncio.sleep(0.1)
            assert [b["prompt"] for b in engine.submitted] == [
                "one"
            ]  # the second has not touched the engine

            engine.release()
            await collect(first)
            await collect(second)
            assert [b["prompt"] for b in engine.submitted] == ["one", "two"]
            assert (
                r.replies(first_chat)[0].status
                == r.replies(second_chat)[0].status
                == "done"
            )
            assert r.jobs.waiting == 0
            r.jobs.admit(third_chat)  # there is room again

    async def test_one_chat_makes_one_image_at_a_time(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4), HOLD, finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            with pytest.raises(Busy, match="already being made in this chat"):
                r.jobs.admit(chat_id)
            with pytest.raises(Busy, match="already being made in this chat"):
                r.jobs.start(plan_for(chat_id))
            engine.release()
            await collect(job)
            r.jobs.admit(chat_id)

    async def test_a_new_chat_has_nothing_pending(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            r.jobs.admit(None)

    async def test_an_image_waits_for_a_reply_in_another_process_to_end(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            chat_id = r.chat()
            with holding(socket_dir, "shared") as other_process:
                job = r.jobs.start(plan_for(chat_id))
                queue = job.subscribe()
                assert (await asyncio.wait_for(queue.get(), 5))["phase"] == "waiting"
                await asyncio.sleep(0.1)
                assert r.engine.calls == [] and not r.replies(chat_id)
                other_process.kill()  # the kernel drops its lock
            events = await collect(job)
            assert events[-1]["t"] == "done"

    async def test_an_image_gives_up_waiting_after_the_limit(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        impatient = Pacing(tick=0.01, lock_wait=0.2, unload_wait=0.3)
        async with rig(tmp_path, socket_dir, pacing=impatient) as r:
            held = r.lock.try_exclusive()
            chat_id = r.chat()
            events = await collect(r.jobs.start(plan_for(chat_id)))
            error = events[-1]["message"]["error"]
            assert error["code"] == "gpu_busy" and "Try again" in error["message"]
            assert r.engine.calls == [] and r.replies(chat_id)[0].status == "error"
            held.release()

    async def test_a_running_image_keeps_replies_out(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4), HOLD, finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            job = r.jobs.start(plan_for(r.chat()))
            await until(phase_of(job, "sampling"))
            with pytest.raises(Busy, match="local models are paused"):
                r.lock.share()
            engine.release()
            await collect(job)
            r.lock.share().release()


@pytest.mark.anyio
class TestShutdown:
    async def test_jobs_are_saved_as_stopped_and_the_engine_is_left_alone(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 4)])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            chat_id = r.chat()
            job = r.jobs.start(plan_for(chat_id))
            await until(phase_of(job, "sampling"))
            calls_before = list(engine.calls)
            await r.jobs.shutdown()
            [stored] = r.replies(chat_id)
            assert (stored.status, stored.images) == ("stopped", None)
            assert (
                engine.calls == calls_before
            )  # no cancel, no kill: it finishes alone and idles out
            assert gpu_is_free(r) and r.jobs.pending(chat_id) is None
            assert (await collect(job))[-1]["message"]["status"] == "stopped"

    async def test_a_job_that_never_got_to_run_is_saved_too(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            chat_id = r.chat()
            r.jobs.start(plan_for(chat_id))
            await r.jobs.shutdown()  # before the task has had a turn
            assert r.replies(chat_id)[0].status == "stopped"
            assert gpu_is_free(r)

    async def test_waiting_jobs_are_saved_and_leave_no_descriptor(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        async with rig(tmp_path, socket_dir) as r:
            held = r.lock.try_exclusive()
            before = open_fds()
            chat_id = r.chat()
            r.jobs.start(plan_for(chat_id))
            await asyncio.sleep(0.05)
            await r.jobs.shutdown()
            assert r.replies(chat_id)[0].status == "stopped"
            assert open_fds() == before
            held.release()

    async def test_the_tasks_are_held_by_the_module_not_only_by_the_loop(
        self, tmp_path: Path, socket_dir: Path
    ) -> None:
        from aihub_web import images

        engine = FakeEngine()
        engine.scripts.append([generating(1, 4), HOLD, finished])
        async with rig(tmp_path, socket_dir, engine=engine) as r:
            job = r.jobs.start(plan_for(r.chat()))
            assert job.task in images._TASKS
            engine.release()
            await collect(job)
            await asyncio.sleep(0)
            assert job.task not in images._TASKS
