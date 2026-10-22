---
status: proposed
date: 2026-10-21
decision-makers: Akhil
---

# Run scheduled tasks inside the web app, on local models only, with no tools

## Context and problem statement

aihub should do what Claude desktop's scheduled tasks do: run a saved prompt on a schedule and leave the reply in a chat (plan: `~/.claude/plans/aihub-projects-and-scheduled-tasks.md`, phases P3 and P4). Nobody watches a run. The web app already runs as a user service (`aihub-web`) and as the desktop app's backend, it has one GPU and a concurrency gate for replies, and its chats live in SQLite. The question is where the scheduler runs, what a task may use, and what a restart, a suspended PC or a change of clock may do to it.

## Decision drivers

- An unattended run must never spend money, send its prompt to a cloud host, or loop.
- A process that dies mid-run and is started again must not run the same occurrence again, and a PC that slept must not pile runs up or make them drift.
- No new unit to install and keep in step: the desktop app runs the same code.
- One run at a time on one GPU, sharing it with replies and with image jobs.
- Wall-clock times must be right across daylight saving changes.

## Considered options

1. A scheduler task in the web app's lifespan, with each task's next run stored in the chat database.
2. A systemd user timer for each task, calling an HTTP endpoint or a command.
3. A separate scheduler daemon.
4. Cron expressions for the schedule instead of a few structured kinds.

## Decision outcome

Chosen option: the scheduler runs in the web app's lifespan (option 1), with structured schedules (not option 4's cron strings), because it needs no unit, uses the gate and the GPU share that replies already use, and keeps the next run next to the task.

- **Local text models only, enforced in code.** `POST` and `PATCH /api/tasks` refuse any model that is not Ollama's (422 `task_model`), `ChatService.prepare` refuses it again whenever it is given a `task_id`, so the scheduler and Run now cannot skip it, and a model that is gone fails the run. There is no fallback to another model.
- **No tools.** A run sends the saved prompt with the standing and project instructions and reads nothing else, so the model never holds private data, untrusted content and an outbound channel at once. If chat ever gets tools or web access, tasks must not inherit them without a per-task approval.
- **Claim first.** A task's next run is set from its schedule, never from its last run, and it is stored before the run starts. A process that dies mid-run and is restarted does not run that occurrence again, and a crash during a run cannot loop. An interrupted run keeps what had arrived (a stopped reply) and is not repeated.
- **Catch-up.** A task due while the app was off runs once if it was due less than 12 hours ago, and is skipped and recorded if it was due longer ago. Missed times collapse into one.
- **Schedules** are every N hours (N divides a day, so the runs sit on the same hours every day), every day, every weekday, or one day a week, on the local wall clock. A wall time is turned into a moment by asking the C library for both daylight readings and keeping the ones that read back unchanged: a time that happens twice runs at the first of them, and a time that is skipped runs when the clock jumps past it.
- **One run at a time**, however it was started. A busy service (replies streaming, an image being made) leaves the task due and counts nothing, so the next 30-second check tries again.
- **Three failures in a row switch a task off**, with the reason on its card; a run that fails, is stopped at the 15-minute limit, or returns no text counts. Switching it on again starts the count afresh.

### Consequences

- Good, because there is no second service: the desktop app and the user service run the same scheduler, and one test suite covers both.
- Good, because the local-only rule is checked at three places that each fail closed, and the scheduler cannot forget it because it sits in the one function a run enters.
- Good, because a restart cannot repeat a run and a clock set back cannot run a task twice.
- Bad, because tasks run only while aihub is running. A task due while it was off is caught up within 12 hours or skipped.
- Bad, because a task due while Ollama is still starting fails with "model unavailable" and counts toward the three; three such days switch it off. Treating "unavailable" like "busy" would hide a model that was really removed.
- Bad, because the stored next run is an absolute moment: after a change of time zone one occurrence runs at the old zone's wall time, then the schedule takes over.
- Bad, because the explanation of a switch-off lives in the same `last_error` column as an ordinary error, and is recognised by how it begins (`switched_off()` in `tasks.py`), not by a column of its own.
- Bad, because the teardown of the app depends on awaiting its background tasks in one order: an extra `await` there lets uvicorn's force-cancelled request log a traceback (`tests/test_web_launch.py` and `tests/test_web_shutdown.py` catch it).

### Confirmation

`tests/test_web_schedule.py` runs the schedule math in real time zones (Los Angeles both ways, a 30-minute change in Lord Howe, a half-hour offset) and checks that a year of runs of every kind comes out in order; a reviewer compared it with a zoneinfo reference across nine zones and eleven schedules over 2027 and 2028 and found no difference. `tests/test_web_tasks.py` covers the run, the scheduler (catch-up, collapse, claim first, one at a time, failures and switch-off, cancellation, a task changed or deleted during its run), and Run now; `tests/test_web_server.py` covers the routes and the lifespan; `tests/e2e/tasks.test.mjs` drives the page. Each of these was checked to fail when the behaviour it protects is removed.

## More information

- The README's "Scheduled tasks" section is the user-facing description. DESIGN.md has the page, the card, the switch, the dialog and the mark.
- Not in this record: projects (plan phases P1 and P2) and the image engine (`0001-image-engine.md`).
