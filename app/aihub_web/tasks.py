"""Scheduled tasks: the run of one, with nobody watching, and the scheduler that starts each when it is due."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from .chat import (
    UNEXPECTED_MESSAGE,
    ApiError,
    ChatRequest,
    ChatService,
    ImageTurn,
    Turn,
)
from .db import Store, Task
from .schedule import next_run, parse_schedule

log = logging.getLogger(__name__)

TICK_SECONDS = 30
RUN_LIMIT_SECONDS = 15 * 60
# A task that is due longer ago than this is skipped rather than run late: a morning digest at midnight is no use.
CATCH_UP_MS = 12 * 3_600_000
MAX_FAILURES = 3
SKIPPED = "Skipped: it was due more than 12 hours before the app could run it."


@dataclass(frozen=True, slots=True)
class RunResult:
    """How one run ended. `busy` means it did not start (replies are streaming, or an image is being made),
    nothing was stored, and the task is simply tried again later."""

    status: Literal["done", "error", "stopped", "busy"]
    chat_id: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Started:
    """A run that was accepted: its chat exists, and the reply waits to be streamed by finish_run."""

    turn: Turn | ImageTurn

    @property
    def chat_id(self) -> str:
        return self.turn.chat.id


def next_run_of(task: Task, after_ms: int) -> int:
    return next_run(parse_schedule(task.schedule), after_ms)


def start_run(service: ChatService, task: Task, day: date) -> Started | RunResult:
    """Make the chat for a run, named "<task>, <date>" and linked to the task. Whatever can be refused is refused
    here, before anything is stored: a model that is gone, or not on this PC, or a service that is full."""
    body = ChatRequest(
        text=task.prompt,
        model=task.model,
        effort=task.effort,
        project_id=task.project_id,
    )
    title = f"{task.name}, {day.day} {day:%b} {day.year}"
    try:
        return Started(service.prepare(body, title=title, task_id=task.id))
    except ApiError as err:
        return RunResult("busy" if err.status == 429 else "error", None, err.message)


async def finish_run(
    service: ChatService, started: Started, limit: float = RUN_LIMIT_SECONDS
) -> RunResult:
    """Stream the reply to its end, within `limit` seconds, through the path a reply typed in the page takes.
    Cancelling the call stops the reply, which keeps what had arrived, and lets the cancel through."""
    last: dict[str, object] = {}
    try:
        async with asyncio.timeout(limit):
            async for event in service.stream(started.turn):
                last = event
    except TimeoutError:
        return RunResult(
            "stopped",
            started.chat_id,
            "Stopped: the run took longer than the time limit.",
        )

    reply = last["message"]
    if last["t"] == "error":
        return RunResult("error", started.chat_id, reply["error"]["message"])
    if not reply["content"].strip():
        return RunResult("error", started.chat_id, "The model gave no answer.")
    return RunResult("done", started.chat_id)


class Scheduler:
    """Starts the tasks that are due, one at a time, and keeps each one's next run and last result.

    A task's next run is stored, set from its schedule and never from its last run, and it is set before the
    run starts: if the process dies during a run, that run does not come round again at the next start.
    """

    def __init__(
        self,
        store: Store,
        service: ChatService,
        *,
        now: Callable[[], float] = time.time,
        tick: float = TICK_SECONDS,
        limit: float = RUN_LIMIT_SECONDS,
    ) -> None:
        self._store = store
        self._service = service
        self._now = now
        self._tick = tick
        self._limit = limit
        self.running: set[str] = set()  # the ids of the tasks that are running now

    def _now_ms(self) -> int:
        return round(self._now() * 1000)

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler pass failed")
            await asyncio.sleep(self._tick)

    async def tick(self) -> None:
        """Run what is due, earliest first. Time passes during a run, so each task is read again before it starts."""
        for due in self._store.due_tasks(self._now_ms()):
            task = self._store.get_task(due.id)
            if (
                task
                and task.enabled
                and task.next_run_at is not None
                and task.next_run_at <= self._now_ms()
            ):
                await self._run_due(task)

    async def _run_due(self, task: Task) -> None:
        now = self._now_ms()
        try:
            following = next_run_of(task, now)
        except ValueError:  # a schedule this code cannot read: left due, it would make a chat every tick
            self._store.update_task(
                task.id,
                {
                    "enabled": False,
                    "next_run_at": None,
                    "last_run_at": now,
                    "last_status": "error",
                    "last_error": "Switched off: its schedule could not be read.",
                },
            )
            return
        assert task.next_run_at is not None, "only a task with a next run is due"
        if now - task.next_run_at > CATCH_UP_MS:
            self._store.update_task(
                task.id,
                {
                    "next_run_at": following,
                    "last_run_at": now,
                    "last_status": "skipped",
                    "last_error": SKIPPED,
                    "last_chat_id": None,
                },
            )
            return

        try:
            started = start_run(
                self._service, task, datetime.fromtimestamp(now / 1000).date()
            )
        except Exception:
            log.exception("task %s could not be started", task.id)
            started = RunResult("error", None, UNEXPECTED_MESSAGE)
        if isinstance(started, RunResult):
            # A busy service leaves the task due, and the next tick tries again.
            if started.status != "busy":
                self._store.update_task(task.id, {"next_run_at": following})
                self._record(task.id, started, now)
            return

        self._store.update_task(task.id, {"next_run_at": following})
        self.running.add(task.id)
        try:
            result = await finish_run(self._service, started, self._limit)
        except Exception:
            log.exception("task %s failed unexpectedly", task.id)
            result = RunResult("error", started.chat_id, UNEXPECTED_MESSAGE)
        finally:
            self.running.discard(task.id)
        self._record(task.id, result, now)

    def _record(self, task_id: str, result: RunResult, ran_at: int) -> None:
        task = self._store.get_task(task_id)
        if task is None:  # deleted during the run: its chat stays, with no task
            return
        changes: dict[str, object] = {
            "last_run_at": ran_at,
            "last_status": result.status,
            "last_error": result.error,
            "last_chat_id": result.chat_id,
        }
        if result.status == "done":
            changes["failures"] = 0
        else:
            failures = task.failures + 1
            changes["failures"] = failures
            if failures >= MAX_FAILURES:
                changes["enabled"] = False
                changes["next_run_at"] = None
                changes["last_error"] = (
                    f"Switched off after {MAX_FAILURES} failed runs in a row. The last one: {result.error}"
                )
        self._store.update_task(task_id, changes)
