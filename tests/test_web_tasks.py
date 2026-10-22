"""Running a scheduled task once: its prompt goes through the chat service, with nobody watching."""

import asyncio
from pathlib import Path

import pytest
from aihub_web.chat import ChatRequest
from aihub_web.config import Limits
from aihub_web.db import Store, Task
from aihub_web.providers import Finish, ProviderError, Text, Thinking
from aihub_web.tasks import RunResult, run_task
from helpers import HANG, HOLD, FakeEngine, finished, generating, make_service

LOCAL = "ollama/qwen3:8b"  # off / on
FREE = "openrouter/google/gemma-4-31b-it:free"
HAIKU = "anthropic/claude-haiku-5-5"
IMAGE = "images/zimage"
DAILY = {"kind": "daily", "time": "08:00"}


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
            "Morning digest",
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
            "Stopped: the run took longer than the time limit.",
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
