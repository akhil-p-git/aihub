// Run with TZ=UTC: node --test tests/js
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  DAY_NAMES,
  REPEATS,
  needsDay,
  needsTime,
  toFields,
  toSchedule,
} from "../../app/aihub_web/static/js/schedule.js";

describe("the repeat choices", () => {
  it("offer every size the server accepts, then day, weekday and week", () => {
    assert.deepEqual(
      REPEATS.map(([value]) => value),
      [
        "hourly:1",
        "hourly:2",
        "hourly:3",
        "hourly:4",
        "hourly:6",
        "hourly:8",
        "hourly:12",
        "daily",
        "weekdays",
        "weekly",
      ],
    );
  });

  it("say each in words", () => {
    const words = Object.fromEntries(REPEATS);
    assert.equal(words["hourly:1"], "Every hour");
    assert.equal(words["hourly:6"], "Every 6 hours");
    assert.equal(words.daily, "Every day");
    assert.equal(words.weekdays, "Every weekday");
    assert.equal(words.weekly, "Every week");
  });

  it("count the week from Monday, as the server does", () => {
    assert.equal(DAY_NAMES[0], "Monday");
    assert.equal(DAY_NAMES[6], "Sunday");
  });
});

describe("schedule and fields", () => {
  const schedules = [
    [
      { kind: "hourly", every: 1 },
      { repeat: "hourly:1", time: "08:00", day: "0" },
    ],
    [
      { kind: "hourly", every: 12 },
      { repeat: "hourly:12", time: "08:00", day: "0" },
    ],
    [
      { kind: "daily", time: "07:45" },
      { repeat: "daily", time: "07:45", day: "0" },
    ],
    [
      { kind: "weekdays", time: "18:30" },
      { repeat: "weekdays", time: "18:30", day: "0" },
    ],
    [
      { kind: "weekly", day: 6, time: "09:00" },
      { repeat: "weekly", time: "09:00", day: "6" },
    ],
  ];

  for (const [schedule, fields] of schedules) {
    it(`${JSON.stringify(schedule)} reads into the editor and comes back the same`, () => {
      assert.deepEqual(toFields(schedule), fields);
      assert.deepEqual(toSchedule(fields), schedule);
    });
  }

  it("start a new task on every day at eight", () => {
    assert.deepEqual(toFields(null), { repeat: "daily", time: "08:00", day: "0" });
  });

  it("send nothing the kind does not use", () => {
    assert.deepEqual(toSchedule({ repeat: "hourly:3", time: "07:00", day: "4" }), { kind: "hourly", every: 3 });
    assert.deepEqual(toSchedule({ repeat: "daily", time: "07:00", day: "4" }), { kind: "daily", time: "07:00" });
  });

  it("know which kinds ask for a time and for a day", () => {
    assert.deepEqual(
      REPEATS.map(([value]) => [value, needsTime(value), needsDay(value)]).filter(([, time]) => !time),
      REPEATS.filter(([value]) => value.startsWith("hourly")).map(([value]) => [value, false, false]),
    );
    assert.equal(needsDay("weekly"), true);
    assert.equal(needsDay("weekdays"), false);
    assert.equal(needsTime("weekly"), true);
  });
});
