"""Scheduled tasks: the run of one, with nobody watching."""

import asyncio
from dataclasses import dataclass
from typing import Literal

from .chat import ApiError, ChatRequest, ChatService
from .db import Task

RUN_LIMIT_SECONDS = 15 * 60


@dataclass(frozen=True, slots=True)
class RunResult:
    """How one run ended. `busy` means it did not start (replies are streaming, or an image is being made),
    nothing was stored, and the task is simply tried again later."""

    status: Literal["done", "error", "stopped", "busy"]
    chat_id: str | None = None
    error: str | None = None


async def run_task(
    service: ChatService, task: Task, *, limit: float = RUN_LIMIT_SECONDS
) -> RunResult:
    """Send the task's prompt in a new chat, through the same path as a reply typed in the page, and wait for the
    end of it. Cancelling the call stops the reply, which keeps what had arrived, and lets the cancel through."""
    body = ChatRequest(
        text=task.prompt,
        model=task.model,
        effort=task.effort,
        project_id=task.project_id,
    )
    try:
        turn = service.prepare(body, title=task.name, task_id=task.id)
    except ApiError as err:
        return RunResult("busy" if err.status == 429 else "error", None, err.message)

    last: dict[str, object] = {}
    try:
        async with asyncio.timeout(limit):
            async for event in service.stream(turn):
                last = event
    except TimeoutError:
        return RunResult(
            "stopped", turn.chat.id, "Stopped: the run took longer than the time limit."
        )

    reply = last["message"]
    if last["t"] == "error":
        return RunResult("error", turn.chat.id, reply["error"]["message"])
    if not reply["content"].strip():
        return RunResult("error", turn.chat.id, "The model gave no answer.")
    return RunResult("done", turn.chat.id)
