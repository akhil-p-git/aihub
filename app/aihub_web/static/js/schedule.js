// How the task editor says a schedule: one "repeat" choice plus a time and a day, and back to what the server takes.

export const HOURS = [1, 2, 3, 4, 6, 8, 12];

/** [value, words] pairs for the Repeat choice. */
export const REPEATS = [
  ...HOURS.map((n) => [`hourly:${n}`, n === 1 ? "Every hour" : `Every ${n} hours`]),
  ["daily", "Every day"],
  ["weekdays", "Every weekday"],
  ["weekly", "Every week"],
];

/** The week counts from Monday, as the server does. */
export const DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

export const needsTime = (repeat) => !repeat.startsWith("hourly");
export const needsDay = (repeat) => repeat === "weekly";

/** The Repeat choice, the time and the day that show a schedule in the editor (null: a new task). */
export function toFields(schedule) {
  const kind = schedule?.kind ?? "daily";
  return {
    repeat: kind === "hourly" ? `hourly:${schedule.every}` : kind,
    time: schedule?.time ?? "08:00",
    day: String(schedule?.day ?? 0),
  };
}

/** The schedule the server takes, from what the editor holds. Only what the kind uses is sent. */
export function toSchedule({ repeat, time, day }) {
  if (repeat.startsWith("hourly:")) return { kind: "hourly", every: Number(repeat.slice("hourly:".length)) };
  if (repeat === "weekly") return { kind: "weekly", day: Number(day), time };
  return { kind: repeat, time };
}
