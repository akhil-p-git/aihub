"""When a task runs: a structured schedule, the next time it is due after a moment, and how to say it in words.

A schedule is one of four kinds, not a cron string: every N hours (N divides a day, so the runs sit on the same
hours every day), every day, every weekday, or one day a week. Times are the PC's local wall clock, turned into
moments with the C library, which knows the system's time zone and its daylight saving rules. A wall time that
happens twice (the hour repeated in autumn) runs at the first of them, once; one that does not exist (the hour
skipped in spring) runs the moment the clock jumps past it.
"""

import time
from datetime import date, datetime, timedelta
from datetime import time as clock_time
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

HOURLY_STEPS = (1, 2, 3, 4, 6, 8, 12)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# Zero-padded, 24-hour, nothing else: "8:00" and "08:00\n" are not times.
HH_MM = r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$"
# A week and a day: the furthest ahead the next run of any kind can be, with room for a clock change.
LOOK_AHEAD_DAYS = 9


class _Kind(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Hourly(_Kind):
    kind: Literal["hourly"]
    every: Literal[1, 2, 3, 4, 6, 8, 12]


class Daily(_Kind):
    kind: Literal["daily"]
    time: str = Field(pattern=HH_MM)


class Weekdays(_Kind):
    kind: Literal["weekdays"]
    time: str = Field(pattern=HH_MM)


class Weekly(_Kind):
    kind: Literal["weekly"]
    day: int = Field(ge=0, le=6, strict=True)  # 0 is Monday
    time: str = Field(pattern=HH_MM)


Schedule = Annotated[Hourly | Daily | Weekdays | Weekly, Field(discriminator="kind")]
_adapter: TypeAdapter[Schedule] = TypeAdapter(Schedule)


def parse_schedule(raw: object) -> Hourly | Daily | Weekdays | Weekly:
    """The schedule a stored or submitted dict describes; raises pydantic's ValidationError if it is none."""
    return _adapter.validate_python(raw)


def _at(day: date, hhmm: str) -> datetime:
    return datetime.combine(day, clock_time(int(hhmm[:2]), int(hhmm[3:])))


def _wall_times(
    schedule: Hourly | Daily | Weekdays | Weekly, day: date
) -> list[datetime]:
    """The wall-clock times a schedule has on one date, earliest first."""
    match schedule:
        case Hourly(every=every):
            return [
                datetime.combine(day, clock_time(hour)) for hour in range(0, 24, every)
            ]
        case Daily(time=hhmm):
            return [_at(day, hhmm)]
        case Weekdays(time=hhmm):
            return [_at(day, hhmm)] if day.weekday() < 5 else []
        case Weekly(day=weekday, time=hhmm):
            return [_at(day, hhmm)] if day.weekday() == weekday else []


def _moment(wall: datetime) -> float:
    """The first moment the local clock reads `wall`, or, when it never does, the first it reads later.

    `mktime` is asked for both readings (as standard and as daylight time) and each is checked by reading the
    clock back: an ordinary wall time has one real reading, a repeated one has two (the first is taken), a
    skipped one has none (the clock jumps somewhere between the two, and the jump is found by halving). Asking for
    one reading with `tm_isdst = -1` is not enough: the C library settles a repeated time from a hint it keeps from
    its previous call, so the same question could be answered with either occurrence."""
    fields = wall.timetuple()[:8]
    readings = sorted({time.mktime((*fields, flag)) for flag in (0, 1)})
    real = [moment for moment in readings if datetime.fromtimestamp(moment) == wall]
    if real:
        return real[0]
    low, high = readings[0], readings[-1]
    while high - low > 0.5:
        middle = (low + high) / 2
        if datetime.fromtimestamp(middle) < wall:
            low = middle
        else:
            high = middle
    return round(high)


def next_run(schedule: Hourly | Daily | Weekdays | Weekly, after_ms: int) -> int:
    """The first moment the schedule is due strictly after `after_ms` (milliseconds since the epoch)."""
    after = after_ms / 1000
    today = datetime.fromtimestamp(after).date()
    for ahead in range(LOOK_AHEAD_DAYS):
        for wall in _wall_times(schedule, today + timedelta(days=ahead)):
            # Two wall times can land on one moment (the skipped hour), so it is strictly after that counts.
            moment = _moment(wall)
            if moment > after:
                return round(moment * 1000)
    raise ValueError("a schedule always has a next run within a week")


def describe(schedule: Hourly | Daily | Weekdays | Weekly) -> str:
    match schedule:
        case Hourly(every=1):
            return "Every hour"
        case Hourly(every=every):
            return f"Every {every} hours"
        case Daily(time=hhmm):
            return f"Every day at {hhmm}"
        case Weekdays(time=hhmm):
            return f"Every weekday at {hhmm}"
        case Weekly(day=weekday, time=hhmm):
            return f"Every {DAYS[weekday]} at {hhmm}"
