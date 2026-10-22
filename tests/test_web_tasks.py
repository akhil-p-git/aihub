"""Scheduled tasks: one run, whose prompt goes through the chat service with nobody watching, and the scheduler
that starts the tasks that are due."""

import asyncio
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest
from aihub_web.chat import ChatRequest, ChatService
from aihub_web.config import Limits
from aihub_web.db import Store, Task
from aihub_web.providers import Finish, ProviderError, Text, Thinking
from aihub_web.tasks import (
    RUN_LIMIT_SECONDS,
    RunResult,
    Scheduler,
    finish_run,
    start_run,
)
from helpers import (
    HANG,
    HOLD,
    Clock,
    FakeEngine,
    finished,
    generating,
    make_service,
    utc_ms,
)

LOCAL = "ollama/qwen3:8b"  # off / on
FREE = "openrouter/google/gemma-4-31b-it:free"
HAIKU = "anthropic/claude-haiku-5-5"
IMAGE = "images/zimage"
DAILY = {"kind": "daily", "time": "08:00"}


DAY = date(2027, 1, 15)


async def run_task(
    service: ChatService, task: Task, *, limit: float = RUN_LIMIT_SECONDS
) -> RunResult:
    """One run from its start to its end, as the scheduler does it."""
    started = start_run(service, task, DAY)
    if isinstance(started, RunResult):
        return started
    return await finish_run(service, started, limit)


def a_task(store: Store, **fields) -> Task:
    fields.setdefault("prompt", "Summarise what I should do today.")
    fields.setdefault("model", LOCAL)
    fields.setdefault("effort", None)
    fields.setdefault("schedule", DAILY)
    return store.create_task("Morning digest", **fields)


@pytest.mark.anyio
class TestRunning:
    async def test_a_run_makes_a_chat_named_for_the_task(self, tmp_path: Path) -> None:
        service, store, providers = await make_service(tmp_path)
        providers["ollama"].script = [Text("Do the "), Text("laundry."), Finish("stop")]
        task = a_task(store)

        result = await run_task(service, task)

        (chat,) = store.list_chats()
        assert result == RunResult("done", chat.id, None)
        assert (chat.title, chat.task_id, chat.model) == (
            "Morning digest, 15 Jan 2027",
            task.id,
            LOCAL,
        )
        assert [(m.role, m.content, m.status) for m in store.messages(chat.id)] == [
            ("user", "Summarise what I should do today.", "done"),
            ("assistant", "Do the laundry.", "done"),
        ]

    async def test_the_tasks_effort_and_project_are_used(self, tmp_path: Path) -> None:
        service, store, providers = await make_service(tmp_path)
        project = store.create_project("Home", "Answer in French.")
        task = a_task(store, effort="on", project_id=project.id)

        result = await run_task(service, task)

        request = providers["ollama"].requests[-1]
        assert request.effort == "on"
        assert 'Instructions for the project "Home"' in request.system
        assert store.get_chat(result.chat_id).project_id == project.id

    async def test_a_failing_provider_is_an_error_with_its_message(
        self, tmp_path: Path
    ) -> None:
        service, store, providers = await make_service(tmp_path)
        providers["ollama"].script = [
            Text("par"),
            ProviderError("Ollama stopped answering.", "upstream"),
        ]

        result = await run_task(service, a_task(store))

        assert (result.status, result.error) == ("error", "Ollama stopped answering.")
        reply = store.messages(result.chat_id)[-1]
        assert (reply.status, reply.content) == ("error", "par")

    async def test_a_reply_with_no_text_is_an_error(self, tmp_path: Path) -> None:
        """A reasoning model can spend its whole allowance thinking. Nobody is there to notice an empty chat."""
        service, store, providers = await make_service(tmp_path)
        providers["ollama"].script = [Thinking("hmm"), Finish("length")]

        result = await run_task(service, a_task(store))

        assert (result.status, result.error) == ("error", "The model gave no answer.")
        assert result.chat_id is not None

    async def test_a_reply_of_only_spaces_is_an_error_too(self, tmp_path: Path) -> None:
        service, store, providers = await make_service(tmp_path)
        providers["ollama"].script = [Text("  \n"), Finish("stop")]
        result = await run_task(service, a_task(store))
        assert (result.status, result.error) == ("error", "The model gave no answer.")

    async def test_it_is_stopped_at_the_time_limit_and_keeps_what_arrived(
        self, tmp_path: Path
    ) -> None:
        service, store, providers = await make_service(
            tmp_path, limits=Limits(max_concurrent=1)
        )
        providers["ollama"].script = [Text("Half an"), HANG]

        result = await run_task(service, a_task(store), limit=0.05)

        assert (result.status, result.error) == (
            "stopped",
            "The run took longer than the time limit.",
        )
        reply = store.messages(result.chat_id)[-1]
        assert (reply.content, reply.status) == ("Half an", "stopped")
        providers["ollama"].script = [Text("ok"), Finish("stop")]
        assert (
            await run_task(service, a_task(store))
        ).status == "done"  # the slot is free

    async def test_the_gpu_is_free_again_after_a_run_that_timed_out(
        self, tmp_path: Path, bound_socket: str
    ) -> None:
        """A local reply holds part of the GPU, and an image waits for all of it: a leak would block images."""
        service, store, providers = await make_service(
            tmp_path, images=FakeEngine(), socket=bound_socket
        )
        providers["ollama"].script = [Text("Half an"), HANG]

        result = await run_task(service, a_task(store), limit=0.05)

        assert result.status == "stopped"
        lease = service.images.gpu.try_exclusive()
        assert lease is not None, "the run still holds the GPU"
        lease.release()
        await service.images.shutdown()

    async def test_cancelling_it_keeps_what_arrived_and_lets_the_cancel_through(
        self, tmp_path: Path
    ) -> None:
        service, store, providers = await make_service(tmp_path)
        started = asyncio.Event()
        providers["ollama"].script = [Text("Half an"), started.set, HANG]

        running = asyncio.create_task(run_task(service, a_task(store)))
        await started.wait()
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running

        (chat,) = store.list_chats()
        reply = store.messages(chat.id)[-1]
        assert (reply.content, reply.status) == ("Half an", "stopped")


@pytest.mark.anyio
class TestWhenItCannotRunNow:
    async def test_busy_while_replies_are_streaming_and_nothing_is_made(
        self, tmp_path: Path
    ) -> None:
        service, store, providers = await make_service(
            tmp_path, limits=Limits(max_concurrent=1)
        )
        started = asyncio.Event()
        providers["ollama"].script = [started.set, HANG]
        first = service.stream(service.prepare(ChatRequest(text="first", model=LOCAL)))
        await anext(first)  # the "chat" event
        pending = asyncio.create_task(anext(first))
        await started.wait()  # inside the provider, holding the only slot

        result = await run_task(service, a_task(store))

        assert (result.status, result.chat_id) == ("busy", None)
        assert len(store.list_chats()) == 1  # the first one only
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

    async def test_busy_while_an_image_is_made(
        self, tmp_path: Path, bound_socket: str
    ) -> None:
        engine = FakeEngine()
        engine.scripts.append([generating(1, 8), HOLD, finished])
        service, store, _ = await make_service(
            tmp_path, images=engine, socket=bound_socket
        )
        making = service.prepare(ChatRequest(text="a fox", model=IMAGE))

        result = await run_task(service, a_task(store))

        assert (result.status, result.chat_id) == ("busy", None)
        assert result.error and "image" in result.error
        assert len(store.list_chats()) == 1
        engine.release()
        [event async for event in service.stream(making)]
        await service.images.shutdown()


@pytest.mark.anyio
class TestWhatIsRefused:
    @pytest.mark.parametrize("model", [FREE, HAIKU])
    async def test_a_cloud_model_is_never_called(
        self, tmp_path: Path, model: str
    ) -> None:
        service, store, providers = await make_service(tmp_path)

        result = await run_task(service, a_task(store, model=model))

        assert result.status == "error" and result.chat_id is None
        assert result.error == (
            "A scheduled task can only use a text model that runs on this PC."
        )
        assert store.list_chats() == []
        assert all(not provider.requests for provider in providers.values())

    async def test_an_ollama_cloud_model_is_never_called(
        self, tmp_path: Path, cloud_model: str
    ) -> None:
        service, store, providers = await make_service(tmp_path)

        result = await run_task(service, a_task(store, model=cloud_model))

        assert (result.status, result.chat_id) == ("error", None)
        assert result.error == (
            "A scheduled task can only use a text model that runs on this PC."
        )
        assert store.list_chats() == []
        assert all(not provider.requests for provider in providers.values())

    async def test_an_image_model_is_refused(
        self, tmp_path: Path, bound_socket: str
    ) -> None:
        engine = FakeEngine()
        service, store, _ = await make_service(
            tmp_path, images=engine, socket=bound_socket
        )
        result = await run_task(service, a_task(store, model=IMAGE))
        assert (result.status, result.chat_id) == ("error", None)
        assert engine.submitted == []
        await service.images.shutdown()

    async def test_a_model_that_is_gone_is_an_error(self, tmp_path: Path) -> None:
        service, store, _ = await make_service(tmp_path)
        result = await run_task(service, a_task(store, model="ollama/not-installed:1b"))
        assert (result.status, result.chat_id) == ("error", None)
        assert result.error == "There is no model called ollama/not-installed:1b."
        assert store.list_chats() == []

    async def test_a_prompt_over_the_limit_is_an_error(self, tmp_path: Path) -> None:
        service, store, _ = await make_service(
            tmp_path, limits=Limits(max_message_chars=10)
        )
        result = await run_task(service, a_task(store, prompt="x" * 11))
        assert (result.status, result.chat_id) == ("error", None)
        assert result.error == "Messages are limited to 10 characters."
        assert store.list_chats() == []


HOURLY = {"kind": "hourly", "every": 1}


@pytest.fixture(autouse=True)
def utc(tz: Callable[[str], None]) -> None:
    tz("UTC")


async def scheduling(tmp_path: Path, **config):
    """A scheduler over a real store and service, on a clock that starts at 2027-01-15 08:00:00 UTC.
    Returns (scheduler, service, store, providers, clock)."""
    clock = Clock()
    service, store, providers = await make_service(tmp_path, clock=clock, **config)
    scheduler = Scheduler(store, service, now=clock, limit=0.2)
    return scheduler, service, store, providers, clock


@pytest.mark.anyio
class TestWhatIsDue:
    async def test_a_task_that_is_not_due_is_left_alone(self, tmp_path: Path) -> None:
        scheduler, _, store, _, _ = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00:01"))
        await scheduler.tick()
        assert store.list_chats() == []
        assert store.get_task(task.id) == task

    async def test_a_task_that_is_off_never_runs(self, tmp_path: Path) -> None:
        scheduler, _, store, _, _ = await scheduling(tmp_path)
        a_task(store, enabled=False, next_run_at=utc_ms("2027-01-15 07:00"))
        await scheduler.tick()
        assert store.list_chats() == []

    async def test_a_due_task_runs_and_its_next_run_comes_from_the_schedule(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, _, clock = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        clock.advance(5)

        await scheduler.tick()

        (chat,) = store.list_chats()
        done = store.get_task(task.id)
        assert (done.last_status, done.last_error, done.failures) == ("done", None, 0)
        assert (done.last_chat_id, done.last_run_at) == (
            chat.id,
            utc_ms("2027-01-15 08:00:05"),
        )
        assert chat.title == "Morning digest, 15 Jan 2027"
        assert done.next_run_at == utc_ms("2027-01-16 08:00")
        await scheduler.tick()  # nothing is due any more
        assert len(store.list_chats()) == 1

    async def test_a_clock_set_back_does_not_run_it_a_second_time(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, _, clock = await scheduling(tmp_path)
        a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        await scheduler.tick()  # runs at 08:00
        clock.advance(-3 * 3600)  # the clock is set back to 05:00 ...
        await scheduler.tick()
        clock.advance(3 * 3600 + 5)  # ... and reaches 08:00 again
        await scheduler.tick()
        assert len(store.list_chats()) == 1

    async def test_every_time_that_was_missed_collapses_into_one_run(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, _, _ = await scheduling(tmp_path)
        task = a_task(
            store, schedule=HOURLY, next_run_at=utc_ms("2027-01-15 02:00")
        )  # six hours ago
        await scheduler.tick()
        assert len(store.list_chats()) == 1
        assert store.get_task(task.id).next_run_at == utc_ms("2027-01-15 09:00")

    async def test_twelve_hours_late_still_runs(self, tmp_path: Path) -> None:
        scheduler, _, store, _, _ = await scheduling(tmp_path)
        a_task(store, next_run_at=utc_ms("2027-01-14 20:00"))  # exactly 12 h before
        await scheduler.tick()
        assert len(store.list_chats()) == 1

    async def test_a_moment_over_twelve_hours_is_skipped_and_recorded(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, _, _ = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-14 20:00") - 1)
        store.update_task(task.id, {"failures": 1})

        await scheduler.tick()

        assert store.list_chats() == []
        skipped = store.get_task(task.id)
        assert (skipped.last_status, skipped.last_chat_id, skipped.failures) == (
            "skipped",
            None,
            1,
        )
        assert skipped.last_error == (
            "It was due more than 12 hours before the app could run it."
        )
        assert skipped.last_run_at == utc_ms("2027-01-15 08:00")
        assert skipped.next_run_at == utc_ms("2027-01-16 08:00")


@pytest.mark.anyio
class TestWhenARunFails:
    async def test_the_failure_is_recorded_and_the_next_run_still_comes(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        providers["ollama"].script = [
            ProviderError("Ollama stopped answering.", "upstream")
        ]
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))

        await scheduler.tick()

        (chat,) = store.list_chats()
        failed = store.get_task(task.id)
        assert (failed.last_status, failed.last_error, failed.failures) == (
            "error",
            "Ollama stopped answering.",
            1,
        )
        assert failed.enabled and failed.last_chat_id == chat.id
        assert failed.next_run_at == utc_ms("2027-01-16 08:00")

    async def test_three_failures_in_a_row_switch_it_off_and_say_why(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, clock = await scheduling(tmp_path)
        providers["ollama"].script = [
            ProviderError("Ollama stopped answering.", "upstream")
        ]
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        for _ in range(3):
            await scheduler.tick()
            clock.advance(days=1)

        off = store.get_task(task.id)
        assert (off.enabled, off.next_run_at, off.failures) == (False, None, 3)
        assert off.last_status == "error"
        assert off.last_error == (
            "Switched off after 3 failed runs in a row. The last one: Ollama stopped answering."
        )
        before = len(store.list_chats())
        clock.advance(days=3)
        await scheduler.tick()
        assert len(store.list_chats()) == before  # and it stays off

    async def test_a_good_run_resets_the_count(self, tmp_path: Path) -> None:
        scheduler, _, store, providers, clock = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        bad = [ProviderError("Ollama stopped answering.", "upstream")]
        good = [Text("fine"), Finish("stop")]
        for script in (bad, bad, good, bad, bad):
            providers["ollama"].script = script
            await scheduler.tick()
            clock.advance(days=1)
        later = store.get_task(task.id)
        assert later.enabled and later.failures == 2

    async def test_a_run_stopped_at_the_time_limit_counts_as_a_failure(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(
            tmp_path
        )  # a limit of 0.2 s
        providers["ollama"].script = [Text("Half an"), HANG]
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))

        await scheduler.tick()

        stopped = store.get_task(task.id)
        assert (stopped.last_status, stopped.failures) == ("stopped", 1)
        assert stopped.last_error == "The run took longer than the time limit."

    async def test_a_cloud_model_is_refused_again_at_run_time_with_no_fallback(
        self, tmp_path: Path
    ) -> None:
        """The API refuses one at creation; a row that got in some other way must still never reach a cloud."""
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        task = a_task(store, model=HAIKU, next_run_at=utc_ms("2027-01-15 08:00"))

        await scheduler.tick()

        refused = store.get_task(task.id)
        assert (refused.last_status, refused.failures) == ("error", 1)
        assert refused.last_error == (
            "A scheduled task can only use a text model that runs on this PC."
        )
        assert store.list_chats() == []
        assert all(not provider.requests for provider in providers.values())

    async def test_a_busy_service_leaves_it_due_and_counts_nothing(
        self, tmp_path: Path
    ) -> None:
        scheduler, service, store, providers, clock = await scheduling(
            tmp_path, limits=Limits(max_concurrent=1)
        )
        started = asyncio.Event()
        providers["ollama"].script = [started.set, HANG]
        first = service.stream(service.prepare(ChatRequest(text="first", model=LOCAL)))
        await anext(first)
        pending = asyncio.create_task(anext(first))
        await started.wait()  # the only slot is taken
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))

        await scheduler.tick()

        waiting = store.get_task(task.id)
        assert (waiting.last_status, waiting.failures) == (None, 0)
        assert waiting.next_run_at == utc_ms("2027-01-15 08:00")  # still due
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        providers["ollama"].script = [Text("fine"), Finish("stop")]
        clock.advance(30)
        await scheduler.tick()  # the next tick
        assert store.get_task(task.id).last_status == "done"

    async def test_a_busy_service_for_more_than_twelve_hours_skips_the_run(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, _, clock = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        clock.advance(12 * 3600 + 30)  # nothing ran it in all that time
        await scheduler.tick()
        assert store.get_task(task.id).last_status == "skipped"


@pytest.mark.anyio
class TestOneAtATime:
    async def test_due_tasks_run_one_after_the_other_earliest_first(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        seen: list[tuple[str, set[str]]] = []
        providers["ollama"].script = [
            lambda: seen.append(("run", set(scheduler.running))),
            Text("fine"),
            Finish("stop"),
        ]
        late = a_task(store, next_run_at=utc_ms("2027-01-15 07:00"))
        early = a_task(store, next_run_at=utc_ms("2027-01-15 06:00"))

        await scheduler.tick()

        assert seen == [("run", {early.id}), ("run", {late.id})]
        assert scheduler.running == set()
        assert [store.get_task(i).last_status for i in (early.id, late.id)] == [
            "done",
            "done",
        ]

    async def test_a_scheduled_run_waits_for_one_that_a_person_started(
        self, tmp_path: Path
    ) -> None:
        """Only one task runs at a time, however it was started: both would be on the GPU at once."""
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        started = asyncio.Event()
        providers["ollama"].script = [Text("Half an"), started.set, HANG]
        by_hand = a_task(store)
        due = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        scheduler.run_now(by_hand)
        await started.wait()

        await scheduler.tick()

        assert len(store.list_chats()) == 1  # the one made by hand
        waiting = store.get_task(due.id)
        assert (waiting.last_status, waiting.failures) == (None, 0)
        assert waiting.next_run_at == utc_ms("2027-01-15 08:00")  # still due
        assert scheduler.running == {by_hand.id}

        await scheduler.stop()
        providers["ollama"].script = [Text("fine"), Finish("stop")]
        await scheduler.tick()  # the hand-started run is over
        assert store.get_task(due.id).last_status == "done"

    async def test_nor_does_it_start_the_same_task_a_second_time(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        started = asyncio.Event()
        providers["ollama"].script = [Text("Half an"), started.set, HANG]
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        scheduler.run_now(task)  # by hand, a moment before it was due
        await started.wait()

        await scheduler.tick()

        assert len(store.list_chats()) == 1
        assert scheduler.running == {task.id}
        await scheduler.stop()

    @pytest.mark.parametrize(
        ("ending", "status", "failures"),
        [
            ([Text("fine"), Finish("stop")], "done", 0),
            ([ProviderError("Gone.", "upstream")], "error", 1),
        ],
    )
    async def test_a_chat_deleted_during_its_run_does_not_lose_the_result(
        self, tmp_path: Path, ending: list, status: str, failures: int
    ) -> None:
        """The reply is still saved, but the task has no chat to link to, and a failure still counts."""
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        providers["ollama"].script = [
            lambda: [store.delete_chat(c.id) for c in store.list_chats()],
            *ending,
        ]

        await scheduler.tick()

        left = store.get_task(task.id)
        assert (left.last_status, left.last_chat_id, left.failures) == (
            status,
            None,
            failures,
        )
        assert left.next_run_at == utc_ms("2027-01-16 08:00")

    async def test_the_next_run_is_set_before_the_reply_starts(
        self, tmp_path: Path
    ) -> None:
        """If the process dies mid-run and is started again, the run must not come round again at once."""
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        during: list[int | None] = []
        providers["ollama"].script = [
            lambda: during.append(store.get_task(task.id).next_run_at),
            Text("fine"),
            Finish("stop"),
        ]
        await scheduler.tick()
        assert during == [utc_ms("2027-01-16 08:00")]

    async def test_cancelling_a_run_is_no_failure_and_it_is_not_repeated(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        started = asyncio.Event()
        providers["ollama"].script = [Text("Half an"), started.set, HANG]

        ticking = asyncio.create_task(scheduler.tick())
        await started.wait()
        ticking.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticking

        left = store.get_task(task.id)
        assert (left.failures, left.last_status) == (0, None)
        assert left.next_run_at == utc_ms("2027-01-16 08:00")
        assert scheduler.running == set()
        (chat,) = store.list_chats()
        assert store.messages(chat.id)[-1].status == "stopped"  # what arrived is kept

    async def test_an_unexpected_error_is_recorded_and_the_others_still_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler, service, store, _, _ = await scheduling(tmp_path)
        broken = a_task(store, next_run_at=utc_ms("2027-01-15 06:00"))
        fine = a_task(store, next_run_at=utc_ms("2027-01-15 07:00"))
        real = service.prepare

        def prepare(body, **kwargs):
            if kwargs["task_id"] == broken.id:
                raise RuntimeError("a bug")
            return real(body, **kwargs)

        monkeypatch.setattr(service, "prepare", prepare)

        await scheduler.tick()

        failed = store.get_task(broken.id)
        assert (failed.last_status, failed.failures) == ("error", 1)
        assert failed.last_error == (
            "The server hit an unexpected error. The service log has the details."
        )
        assert store.get_task(fine.id).last_status == "done"

    async def test_a_task_switched_off_while_an_earlier_one_runs_is_not_started(
        self, tmp_path: Path
    ) -> None:
        """A run takes minutes, so the list of what was due is out of date by the time its turn comes."""
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        first = a_task(store, next_run_at=utc_ms("2027-01-15 06:00"))
        second = a_task(store, next_run_at=utc_ms("2027-01-15 07:00"))
        providers["ollama"].script = [
            lambda: store.update_task(second.id, {"enabled": False}),
            Text("fine"),
            Finish("stop"),
        ]

        await scheduler.tick()

        assert store.get_task(first.id).last_status == "done"
        assert store.get_task(second.id).last_status is None
        assert len(store.list_chats()) == 1

    async def test_a_schedule_that_cannot_be_read_switches_the_task_off(
        self, tmp_path: Path
    ) -> None:
        """Left due, it would make a chat on every tick."""
        scheduler, _, store, _, _ = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        store.update_task(task.id, {"schedule": {"kind": "fortnightly"}})

        await scheduler.tick()

        off = store.get_task(task.id)
        assert (off.enabled, off.next_run_at, off.last_status) == (False, None, "error")
        assert off.last_error == "Switched off: its schedule could not be read."
        assert store.list_chats() == []

    async def test_a_failure_inside_the_service_is_recorded_with_its_chat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        scheduler, service, store, _, _ = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))

        async def stream(turn):
            raise RuntimeError("a bug")
            yield

        monkeypatch.setattr(service, "stream", stream)

        await scheduler.tick()

        (chat,) = store.list_chats()
        failed = store.get_task(task.id)
        assert (failed.last_status, failed.last_chat_id, failed.failures) == (
            "error",
            chat.id,
            1,
        )
        assert scheduler.running == set()

    @pytest.mark.parametrize(
        "ending", [[Text("fine"), Finish("stop")], [ProviderError("Gone.", "upstream")]]
    )
    async def test_a_task_deleted_during_its_run_leaves_only_its_chat(
        self, tmp_path: Path, ending: list
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        providers["ollama"].script = [lambda: store.delete_task(task.id), *ending]
        await scheduler.tick()
        assert store.get_task(task.id) is None
        (chat,) = store.list_chats()
        assert chat.task_id is None

    async def test_a_task_switched_off_during_its_run_stays_off(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        providers["ollama"].script = [
            lambda: store.update_task(task.id, {"enabled": False, "next_run_at": None}),
            Text("fine"),
            Finish("stop"),
        ]
        await scheduler.tick()
        off = store.get_task(task.id)
        assert (off.enabled, off.next_run_at, off.last_status) == (False, None, "done")


@pytest.mark.anyio
class TestTheLoop:
    async def test_it_ticks_until_cancelled_and_survives_a_failing_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        clock = Clock()
        service, store, _ = await make_service(tmp_path, clock=clock)
        scheduler = Scheduler(store, service, now=clock, tick=0.01)
        task = a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))
        real = store.due_tasks
        calls = []

        def due_tasks(now_ms: int):
            calls.append(now_ms)
            if len(calls) == 1:
                raise RuntimeError("the database is busy")
            return real(now_ms)

        monkeypatch.setattr(store, "due_tasks", due_tasks)

        looping = asyncio.create_task(scheduler.run_forever())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if store.get_task(task.id).last_status == "done":
                break
        looping.cancel()
        with pytest.raises(asyncio.CancelledError):
            await looping

        assert store.get_task(task.id).last_status == "done"
        assert len(calls) >= 2


async def settled(scheduler: Scheduler) -> None:
    """Wait for the runs that people started to finish."""
    for _ in range(300):
        if not scheduler.running:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("a run never finished")


@pytest.mark.anyio
class TestRunNow:
    async def test_the_chat_exists_at_once_and_the_reply_finishes_in_the_background(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        providers["ollama"].script = [Text("Do the laundry."), Finish("stop")]
        task = a_task(store, next_run_at=utc_ms("2027-01-16 08:00"))

        started = scheduler.run_now(task)

        assert not isinstance(started, RunResult)
        chat = store.get_chat(started.chat_id)
        assert (chat.title, chat.task_id) == ("Morning digest, 15 Jan 2027", task.id)
        assert scheduler.running == {task.id}

        await settled(scheduler)

        done = store.get_task(task.id)
        assert (done.last_status, done.last_chat_id) == ("done", chat.id)
        assert store.messages(chat.id)[-1].content == "Do the laundry."
        assert done.next_run_at == utc_ms(
            "2027-01-16 08:00"
        )  # the schedule is left alone

    async def test_a_failure_counts_like_any_other(self, tmp_path: Path) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        providers["ollama"].script = [ProviderError("Gone.", "upstream")]
        task = a_task(store, next_run_at=utc_ms("2027-01-16 08:00"))
        scheduler.run_now(task)
        await settled(scheduler)
        failed = store.get_task(task.id)
        assert (failed.last_status, failed.last_error, failed.failures) == (
            "error",
            "Gone.",
            1,
        )

    async def test_only_one_task_runs_at_a_time(self, tmp_path: Path) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        providers["ollama"].script = [Text("Half an"), HANG]
        first = a_task(store)
        second = a_task(store)

        assert not isinstance(scheduler.run_now(first), RunResult)
        for task in (first, second):  # the same task, and another
            refused = scheduler.run_now(task)
            assert isinstance(refused, RunResult) and refused.status == "busy"
            assert refused.error == (
                "Another scheduled task is running. Try again when it finishes."
            )
        assert len(store.list_chats()) == 1
        await scheduler.stop()

    async def test_a_manual_run_waits_for_a_scheduled_one(self, tmp_path: Path) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        other = a_task(store)
        seen: list[object] = []
        providers["ollama"].script = [
            lambda: seen.append(scheduler.run_now(other)),
            Text("fine"),
            Finish("stop"),
        ]
        a_task(store, next_run_at=utc_ms("2027-01-15 08:00"))

        await scheduler.tick()

        assert [r.status for r in seen if isinstance(r, RunResult)] == ["busy"]

    async def test_a_refusal_is_returned_and_nothing_is_stored_or_counted(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        task = a_task(store, model=HAIKU)

        refused = scheduler.run_now(task)

        assert isinstance(refused, RunResult) and refused.status == "error"
        assert refused.error == (
            "A scheduled task can only use a text model that runs on this PC."
        )
        assert store.list_chats() == [] and scheduler.running == set()
        assert store.get_task(task.id).failures == 0
        assert all(not provider.requests for provider in providers.values())

    async def test_a_chat_deleted_during_a_manual_run_does_not_kill_its_job(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        providers["ollama"].script = [
            lambda: [store.delete_chat(c.id) for c in store.list_chats()],
            Text("fine"),
            Finish("stop"),
        ]
        task = a_task(store, next_run_at=utc_ms("2027-01-16 08:00"))

        scheduler.run_now(task)
        await settled(scheduler)

        done = store.get_task(task.id)
        assert (done.last_status, done.last_chat_id) == ("done", None)

    async def test_stopping_cancels_a_run_and_keeps_what_arrived(
        self, tmp_path: Path
    ) -> None:
        scheduler, _, store, providers, _ = await scheduling(tmp_path)
        started = asyncio.Event()
        providers["ollama"].script = [Text("Half an"), started.set, HANG]
        task = a_task(store)
        scheduler.run_now(task)
        await started.wait()

        await scheduler.stop()

        (chat,) = store.list_chats()
        assert store.messages(chat.id)[-1].status == "stopped"
        assert scheduler.running == set()
        left = store.get_task(task.id)
        assert (left.failures, left.last_status) == (
            0,
            None,
        )  # closing the app is no failure
