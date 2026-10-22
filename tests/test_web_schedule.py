"""Schedules: what is accepted, when each kind is next due (in real time zones, daylight saving included), and how it reads."""

import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import pytest
from aihub_web.schedule import describe, next_run, parse_schedule
from pydantic import ValidationError

ZONEINFO = Path("/usr/share/zoneinfo")
LA = "America/Los_Angeles"


@pytest.fixture
def tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[str], None]]:
    """Sets the process time zone for one test (the schedule math reads the system's, through the C library)."""

    def use(name: str) -> None:
        if not (ZONEINFO / name).exists():
            pytest.skip(f"no zone database for {name}")
        monkeypatch.setenv("TZ", name)
        time.tzset()

    yield use
    monkeypatch.undo()
    time.tzset()


def ms(wall: str) -> int:
    """Milliseconds since the epoch of a local wall time like '2026-03-08 01:59' in the current zone."""
    return int(time.mktime(datetime.fromisoformat(wall).timetuple()) * 1000)


def wall(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000).strftime("%Y-%m-%d %H:%M")


def daily(at: str = "08:00") -> dict:
    return {"kind": "daily", "time": at}


class TestWhatIsAccepted:
    @pytest.mark.parametrize(
        "raw",
        [
            {"kind": "hourly", "every": 1},
            {"kind": "hourly", "every": 12},
            {"kind": "daily", "time": "00:00"},
            {"kind": "daily", "time": "23:59"},
            {"kind": "weekdays", "time": "08:00"},
            {"kind": "weekly", "day": 0, "time": "08:00"},
            {"kind": "weekly", "day": 6, "time": "17:30"},
        ],
    )
    def test_good_schedules(self, raw: dict) -> None:
        assert parse_schedule(raw).model_dump() == raw

    @pytest.mark.parametrize(
        "raw",
        [
            {},
            {"kind": "monthly", "time": "08:00"},
            {"kind": "hourly"},
            {"kind": "hourly", "every": 5},  # does not divide a day
            {"kind": "hourly", "every": 7},
            {"kind": "hourly", "every": 0},
            {"kind": "hourly", "every": 24},
            {"kind": "hourly", "every": "2"},
            {"kind": "daily"},
            {"kind": "daily", "time": "8:00"},
            {"kind": "daily", "time": "24:00"},
            {"kind": "daily", "time": "08:60"},
            {"kind": "daily", "time": "08:00:00"},
            {"kind": "daily", "time": "08:00\n"},
            {"kind": "daily", "time": " 08:00"},
            {"kind": "daily", "time": "08:00", "every": 2},
            {"kind": "weekly", "time": "08:00"},
            {"kind": "weekly", "day": 7, "time": "08:00"},
            {"kind": "weekly", "day": -1, "time": "08:00"},
            {"kind": "weekly", "day": "mon", "time": "08:00"},
            {"kind": "weekdays", "time": "08:00", "day": 1},
        ],
    )
    def test_bad_schedules(self, raw: dict) -> None:
        with pytest.raises(ValidationError):
            parse_schedule(raw)


class TestDaily:
    def test_before_the_time_it_is_today_and_at_or_after_it_is_tomorrow(
        self, tz
    ) -> None:
        tz("UTC")
        schedule = parse_schedule(daily("08:00"))
        assert wall(next_run(schedule, ms("2026-10-21 07:59"))) == "2026-10-21 08:00"
        assert wall(next_run(schedule, ms("2026-10-21 08:00"))) == "2026-10-22 08:00"
        assert wall(next_run(schedule, ms("2026-10-21 08:01"))) == "2026-10-22 08:00"
        assert wall(next_run(schedule, ms("2026-10-21 23:59"))) == "2026-10-22 08:00"

    def test_it_is_strictly_after_even_by_a_millisecond(self, tz) -> None:
        tz("UTC")
        schedule = parse_schedule(daily("08:00"))
        due = ms("2026-10-21 08:00")
        assert next_run(schedule, due - 1) == due
        assert next_run(schedule, due) == due + 86_400_000

    def test_months_and_years_roll_over(self, tz) -> None:
        tz("UTC")
        schedule = parse_schedule(daily("00:05"))
        assert wall(next_run(schedule, ms("2026-12-31 23:00"))) == "2027-01-01 00:05"
        assert (
            wall(next_run(schedule, ms("2028-02-28 12:00"))) == "2028-02-29 00:05"
        )  # a leap year


class TestWeekdaysAndWeekly:
    def test_weekdays_skip_the_weekend(self, tz) -> None:
        tz("UTC")
        schedule = parse_schedule({"kind": "weekdays", "time": "08:00"})
        friday = "2026-10-23"
        assert wall(next_run(schedule, ms(f"{friday} 07:00"))) == "2026-10-23 08:00"
        assert (
            wall(next_run(schedule, ms(f"{friday} 09:00"))) == "2026-10-26 08:00"
        )  # Monday
        assert (
            wall(next_run(schedule, ms("2026-10-24 12:00"))) == "2026-10-26 08:00"
        )  # Saturday
        assert (
            wall(next_run(schedule, ms("2026-10-25 23:59"))) == "2026-10-26 08:00"
        )  # Sunday

    def test_weekly_is_one_day_a_week(self, tz) -> None:
        tz("UTC")
        monday = parse_schedule({"kind": "weekly", "day": 0, "time": "09:30"})
        assert (
            wall(next_run(monday, ms("2026-10-21 12:00"))) == "2026-10-26 09:30"
        )  # from a Wednesday
        assert wall(next_run(monday, ms("2026-10-26 09:29"))) == "2026-10-26 09:30"
        assert wall(next_run(monday, ms("2026-10-26 09:30"))) == "2026-11-02 09:30"
        sunday = parse_schedule({"kind": "weekly", "day": 6, "time": "23:00"})
        assert wall(next_run(sunday, ms("2026-10-25 23:30"))) == "2026-11-01 23:00"


class TestHourly:
    def test_every_six_hours_is_on_the_quarters_of_the_day(self, tz) -> None:
        tz("UTC")
        schedule = parse_schedule({"kind": "hourly", "every": 6})
        walls = []
        at = ms("2026-10-21 00:00")
        for _ in range(5):
            at = next_run(schedule, at)
            walls.append(wall(at))
        assert walls == [
            "2026-10-21 06:00",
            "2026-10-21 12:00",
            "2026-10-21 18:00",
            "2026-10-22 00:00",
            "2026-10-22 06:00",
        ]

    def test_every_hour_and_every_twelve(self, tz) -> None:
        tz("UTC")
        hourly = parse_schedule({"kind": "hourly", "every": 1})
        assert wall(next_run(hourly, ms("2026-10-21 10:30"))) == "2026-10-21 11:00"
        assert wall(next_run(hourly, ms("2026-10-21 23:30"))) == "2026-10-22 00:00"
        twelve = parse_schedule({"kind": "hourly", "every": 12})
        assert wall(next_run(twelve, ms("2026-10-21 00:00"))) == "2026-10-21 12:00"
        assert wall(next_run(twelve, ms("2026-10-21 12:00"))) == "2026-10-22 00:00"


class TestOtherZones:
    @pytest.mark.parametrize(
        "zone",
        ["Asia/Kolkata", "Pacific/Auckland", "America/St_Johns", "Europe/London"],
    )
    def test_the_local_time_is_the_zones_whatever_its_offset(
        self, tz, zone: str
    ) -> None:
        tz(zone)
        schedule = parse_schedule(daily("08:00"))
        assert wall(next_run(schedule, ms("2026-07-01 07:00"))) == "2026-07-01 08:00"
        assert wall(next_run(schedule, ms("2026-07-01 08:00"))) == "2026-07-02 08:00"


class TestDaylightSaving:
    """America/Los_Angeles: 2026-03-08 02:00 jumps to 03:00 (spring), and 2026-11-01 02:00 falls back to 01:00."""

    def test_a_time_that_does_not_exist_runs_the_moment_the_clock_passes_it(
        self, tz
    ) -> None:
        tz(LA)
        schedule = parse_schedule(daily("02:30"))
        assert wall(next_run(schedule, ms("2026-03-07 03:00"))) == "2026-03-08 03:00"
        # and the day after is normal again
        assert wall(next_run(schedule, ms("2026-03-08 04:00"))) == "2026-03-09 02:30"

    def test_the_gap_can_be_half_an_hour(self, tz) -> None:
        tz("Australia/Lord_Howe")  # clocks go from 02:00 to 02:30 on 2026-10-04
        schedule = parse_schedule(daily("02:15"))
        assert wall(next_run(schedule, ms("2026-10-03 12:00"))) == "2026-10-04 02:30"
        assert wall(next_run(schedule, ms("2026-10-04 03:00"))) == "2026-10-05 02:15"

    @pytest.mark.parametrize("remembered", ["2026-07-01 12:00", "2026-12-01 12:00"])
    def test_a_repeated_time_does_not_depend_on_what_was_asked_before(
        self, tz, remembered: str
    ) -> None:
        """The C library resolves a repeated wall time from a hint it keeps from its previous call: the answer must
        not follow that hint (it once made a daily 01:30 run twice, an hour apart)."""
        tz(LA)
        schedule = parse_schedule(daily("01:30"))
        next_run(
            parse_schedule(daily("08:00")), ms(remembered)
        )  # leaves summer or winter as the hint
        first = next_run(schedule, ms("2026-10-31 12:00"))
        assert first == int(
            datetime.fromisoformat("2026-11-01T08:30:00+00:00").timestamp() * 1000
        ), "the first of the two 01:30s, 01:30 PDT"
        assert next_run(schedule, first) - first == 25 * 3_600_000

    def test_the_last_minute_of_the_repeated_hour_is_found_too(self, tz) -> None:
        tz(LA)
        schedule = parse_schedule(daily("01:59"))
        first = next_run(schedule, ms("2026-10-31 12:00"))
        assert first == int(
            datetime.fromisoformat("2026-11-01T08:59:00+00:00").timestamp() * 1000
        )
        assert next_run(schedule, first) - first == 25 * 3_600_000

    def test_a_time_that_happens_twice_runs_once(self, tz) -> None:
        tz(LA)
        schedule = parse_schedule(daily("01:30"))
        first = next_run(schedule, ms("2026-10-31 12:00"))
        assert wall(first) == "2026-11-01 01:30"
        # The next run is the following day's, not the repeated hour an hour later.
        assert wall(next_run(schedule, first)) == "2026-11-02 01:30"
        assert next_run(schedule, first) - first >= 24 * 3_600_000

    def test_every_hour_across_the_spring_gap_has_no_double_run(self, tz) -> None:
        tz(LA)
        schedule = parse_schedule({"kind": "hourly", "every": 1})
        runs = []
        at = ms("2026-03-08 00:30")
        for _ in range(5):
            at = next_run(schedule, at)
            runs.append(wall(at))
        assert runs == [
            "2026-03-08 01:00",
            "2026-03-08 03:00",  # 02:00 does not exist
            "2026-03-08 04:00",
            "2026-03-08 05:00",
            "2026-03-08 06:00",
        ]

    def test_every_hour_across_the_autumn_repeat_runs_each_wall_hour_once(
        self, tz
    ) -> None:
        tz(LA)
        schedule = parse_schedule({"kind": "hourly", "every": 1})
        runs = []
        at = ms("2026-11-01 00:30")
        for _ in range(4):
            at = next_run(schedule, at)
            runs.append(wall(at))
        assert runs == [
            "2026-11-01 01:00",
            "2026-11-01 02:00",
            "2026-11-01 03:00",
            "2026-11-01 04:00",
        ]

    def test_a_time_clear_of_the_change_does_not_move(self, tz) -> None:
        tz(LA)
        schedule = parse_schedule(daily("08:00"))
        for day in ("2026-03-07", "2026-03-08", "2026-11-01", "2026-11-02"):
            assert wall(next_run(schedule, ms(f"{day} 00:00"))) == f"{day} 08:00"


class TestAnyMomentHasAnOrderedNext:
    @pytest.mark.parametrize(
        "raw",
        [
            {"kind": "hourly", "every": 4},
            {"kind": "daily", "time": "02:30"},
            {"kind": "weekdays", "time": "01:30"},
            {"kind": "weekly", "day": 6, "time": "02:15"},
        ],
    )
    def test_the_runs_of_a_year_are_strictly_increasing_and_not_far_apart(
        self, tz, raw: dict
    ) -> None:
        tz(LA)
        schedule = parse_schedule(raw)
        at = ms("2026-01-01 00:00")
        end = ms("2027-01-01 00:00")
        gaps = []
        while at < end:
            following = next_run(schedule, at)
            assert following > at
            gaps.append(following - at)
            at = following
        longest = {"hourly": 4, "daily": 24, "weekdays": 72, "weekly": 168}[raw["kind"]]
        assert (
            max(gaps) <= (longest + 2) * 3_600_000
        )  # an hour of slack either way for the clock change


class TestInWords:
    @pytest.mark.parametrize(
        ("raw", "words"),
        [
            ({"kind": "hourly", "every": 1}, "Every hour"),
            ({"kind": "hourly", "every": 6}, "Every 6 hours"),
            ({"kind": "daily", "time": "08:00"}, "Every day at 08:00"),
            ({"kind": "weekdays", "time": "07:45"}, "Every weekday at 07:45"),
            ({"kind": "weekly", "day": 0, "time": "09:00"}, "Every Monday at 09:00"),
            ({"kind": "weekly", "day": 6, "time": "18:30"}, "Every Sunday at 18:30"),
        ],
    )
    def test_each_kind(self, raw: dict, words: str) -> None:
        assert describe(parse_schedule(raw)) == words
