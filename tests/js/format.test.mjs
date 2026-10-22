// Run with TZ=UTC: node --test tests/js
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  barsFor,
  coerceEffort,
  daysLeft,
  estimateTokens,
  formatBytes,
  formatContext,
  formatCost,
  formatDuration,
  formatPrice,
  formatPriceLong,
  formatSpeed,
  formatWhen,
  groupChats,
  levelLabel,
  routeFor,
  shapeNote,
  shortName,
  taskStatus,
  taskNext,
} from "../../app/aihub_web/static/js/format.js";

const DAY = 86_400_000;
const NOW = Date.UTC(2026, 9, 20, 15, 30); // Tue 20 Oct 2026, 15:30 UTC

describe("barsFor", () => {
  const cases = [
    [["off", "on"], "off", 0],
    [["off", "on"], "on", 4],
    [["off", "low", "high", "max"], "low", 2], // three active levels share four bars; the top one is always full
    [["off", "low", "high", "max"], "high", 3],
    [["off", "low", "high", "max"], "max", 4],
    [["low", "medium", "high"], "low", 2],
    [["low", "medium", "high"], "high", 4],
    [["off", "low", "medium", "high", "xhigh", "max"], "low", 1],
    [["off", "low", "medium", "high", "xhigh", "max"], "max", 4],
  ];
  for (const [levels, level, expected] of cases) {
    it(`${level} of [${levels}] lights ${expected}`, () => assert.equal(barsFor(level, levels), expected));
  }
  it("never lights a bar for off, null or an unknown level", () => {
    assert.equal(barsFor("off", ["off", "low"]), 0);
    assert.equal(barsFor(null, ["low"]), 0);
    assert.equal(barsFor("warp", ["low", "high"]), 0);
  });
  it("is monotonic over every level a model can have", () => {
    const levels = ["off", "minimal", "low", "medium", "high", "xhigh", "max"];
    const lit = levels.map((l) => barsFor(l, levels));
    assert.deepEqual(
      lit,
      [...lit].sort((a, b) => a - b),
    );
    assert.equal(lit.at(-1), 4);
  });
});

describe("groupChats", () => {
  const at = (hoursAgo) => ({ id: String(hoursAgo), updated_at: NOW - hoursAgo * 3_600_000 });
  const labels = (chats) => groupChats(chats, NOW).map((g) => `${g.label}:${g.items.map((c) => c.id)}`);

  it("groups by local day, newest first, skipping empty groups", () => {
    assert.deepEqual(labels([at(1), at(15), at(16), at(40), at(24 * 6), at(24 * 8), at(24 * 29), at(24 * 31)]), [
      "Today:1,15",
      "Yesterday:16",
      "Previous 7 days:40,144",
      "Previous 30 days:192,696",
      "Older:744",
    ]);
  });
  it("draws the line at local midnight", () => {
    const midnight = Date.UTC(2026, 9, 20);
    assert.equal(groupChats([{ id: "a", updated_at: midnight }], NOW)[0].label, "Today");
    assert.equal(groupChats([{ id: "b", updated_at: midnight - 1 }], NOW)[0].label, "Yesterday");
  });
  it("returns nothing for no chats", () => assert.deepEqual(groupChats([], NOW), []));
});

describe("daysLeft", () => {
  it("counts whole days and rounds up", () => {
    assert.equal(daysLeft(NOW, 30, NOW), 30);
    assert.equal(daysLeft(NOW - 29 * DAY - 1000, 30, NOW), 1);
    assert.equal(daysLeft(NOW - 23 * DAY, 30, NOW), 7);
  });
  it("never goes below zero", () => assert.equal(daysLeft(NOW - 60 * DAY, 30, NOW), 0));
});

describe("number formats", () => {
  it("context sizes", () => {
    assert.equal(formatContext(8192), "8K");
    assert.equal(formatContext(40960), "40K");
    assert.equal(formatContext(262144), "256K");
    assert.equal(formatContext(1_000_000), "1M");
    assert.equal(formatContext(1_048_576), "1M");
    assert.equal(formatContext(2_000_000), "2M");
    assert.equal(formatContext(null), "");
  });
  it("cost keeps two significant digits however small", () => {
    assert.equal(formatCost("0.00035"), "$0.00035");
    assert.equal(formatCost("0.0000123"), "$0.000012");
    assert.equal(formatCost("0.5"), "$0.50");
    assert.equal(formatCost("1.234"), "$1.23");
    assert.equal(formatCost("0"), "$0");
    assert.equal(formatCost("nope"), "");
  });
  it("durations", () => {
    assert.equal(formatDuration(420), "420 ms");
    assert.equal(formatDuration(3500), "3.5 s");
    assert.equal(formatDuration(14200), "14 s");
    assert.equal(formatDuration(65_000), "1 min 5 s");
  });
  it("speed, size and price", () => {
    assert.equal(formatSpeed(41.2), "41 tok/s");
    assert.equal(formatSpeed(4.26), "4.3 tok/s");
    assert.equal(formatBytes(5_200_000_000), "5.2 GB");
    assert.equal(formatPrice({ in: "0.15", out: "0.6" }), "$0.15 in, $0.60 out");
    assert.equal(formatPrice({ in: "2", out: "10" }), "$2 in, $10 out");
    assert.equal(formatPrice({ in: "0.005", out: "0.02" }), "$0.0050 in, $0.02 out");
    assert.equal(formatPriceLong({ in: "2", out: "10" }), "$2 in, $10 out per million tokens");
    assert.equal(formatPrice(null), "");
  });
});

describe("names and levels", () => {
  it("drops a vendor prefix but nothing else", () => {
    assert.equal(shortName("Google: Gemma 4 31B"), "Gemma 4 31B");
    assert.equal(shortName("DeepSeek: DeepSeek V4.1 Flash 1017"), "DeepSeek V4.1 Flash 1017");
    assert.equal(shortName("qwen3:8b"), "qwen3:8b");
    assert.equal(shortName("Haiku 5.5"), "Haiku 5.5");
  });
  it("labels every level the server can send", () => {
    assert.deepEqual(["off", "on", "xhigh", "max"].map(levelLabel), ["Off", "On", "Extra high", "Max"]);
    assert.equal(levelLabel("custom"), "custom");
  });
  it("keeps an effort the model offers, else uses its default", () => {
    const model = { efforts: { levels: ["off", "low", "high"], default: "low" } };
    assert.equal(coerceEffort(model, "high"), "high");
    assert.equal(coerceEffort(model, "max"), "low");
    assert.equal(coerceEffort(model, null), "low");
    assert.equal(coerceEffort({ efforts: null }, "high"), null);
    assert.equal(coerceEffort(undefined, "high"), null);
  });
});

describe("routeFor: where the next prompt goes", () => {
  it("local models stay on the PC", () => {
    assert.match(routeFor({ name: "qwen3:8b", tier: "local" }).text, /Nothing leaves the machine/);
  });
  it("free models warn that the host may keep prompts, and say when they end", () => {
    const route = routeFor({
      name: "Google: Gemma 4 31B",
      tier: "free",
      provider: "openrouter",
      expires: "2026-10-31",
    });
    assert.equal(route.title, "New chat on Gemma 4 31B");
    assert.match(route.text, /may keep what you send/);
    assert.match(route.text, /Free until/);
  });
  it("paid models state the price", () => {
    const claude = routeFor({
      name: "Haiku 5.5",
      tier: "paid",
      provider: "anthropic",
      price: { in: "0.10", out: "0.50" },
    });
    assert.match(claude.text, /Claude API credits at \$0\.10 in, \$0\.50 out per million tokens/);
    const router = routeFor({
      name: "DeepSeek V4",
      tier: "paid",
      provider: "openrouter",
      price: { in: "0.15", out: "0.6" },
    });
    assert.match(router.text, /don't keep prompts/);
  });
  it("image models make an image on the PC and say that local chat pauses meanwhile", () => {
    const route = routeFor({ name: "Z-Image Turbo", tier: "local", provider: "images", output: "image" });
    assert.equal(route.title, "Image mode on Z-Image Turbo");
    assert.equal(
      route.text,
      "Your prompt makes one image on this PC. Nothing leaves the machine; local chat models pause while it works.",
    );
    assert.doesNotMatch(route.text, /Ollama/, "an image model does not run through Ollama");
  });
  it("says what to do when no model is usable, in words that are true for the service and the app", () => {
    assert.equal(
      routeFor(undefined).text,
      "Start Ollama, or set an OpenRouter key in ~/.config/aihub/aihub.env, then restart aihub.",
    );
  });
});

describe("shapeNote: what a shape costs in time", () => {
  const model = { est_s: { square: 23, wide: 12, tall: 12 } };
  it("names the shape and the model's estimate for it", () => {
    assert.equal(shapeNote(model, "square"), "Square, about 23 s");
    assert.equal(shapeNote(model, "tall"), "Tall, about 12 s");
  });
  it("names only the shape when the model has no estimate for it", () => {
    assert.equal(shapeNote({ est_s: { square: 23 } }, "wide"), "Wide");
    assert.equal(shapeNote(undefined, "wide"), "Wide");
  });
});

describe("estimateTokens: the server's rule, so the project dialog counts what is budgeted", () => {
  it("is three characters to a token, plus one", () => {
    assert.equal(estimateTokens(""), 1);
    assert.equal(estimateTokens("ab"), 1);
    assert.equal(estimateTokens("abc"), 2);
    assert.equal(estimateTokens("x".repeat(300)), 101);
    assert.equal(estimateTokens("x".repeat(4000)), 1334);
  });
  it("counts characters, not UTF-16 units: an emoji is one", () => {
    assert.equal(estimateTokens("😀😀😀"), 2);
    assert.equal(estimateTokens("é".repeat(6)), 3);
  });
});

describe("formatWhen", () => {
  const at = (...parts) => Date.UTC(...parts);
  const cases = [
    [at(2026, 9, 20, 8, 5), "today 8:05 AM"], // the same day, earlier than now
    [at(2026, 9, 20, 23, 59), "today 11:59 PM"],
    [at(2026, 9, 20, 12, 0), "today 12:00 PM"], // noon is 12 PM
    [at(2026, 9, 20, 0, 30), "today 12:30 AM"], // and just after midnight is 12 AM,
    [at(2026, 9, 21, 0, 0), "tomorrow 12:00 AM"],
    [at(2026, 9, 19, 22, 0), "yesterday 10:00 PM"],
    [at(2026, 9, 22, 7, 0), "Thu 7:00 AM"], // within the next week: the day of the week
    [at(2026, 9, 26, 7, 0), "Mon 7:00 AM"],
    [at(2026, 9, 27, 7, 0), "27 Oct 7:00 AM"], // a week or more ahead: the date
    [at(2026, 9, 12, 9, 30), "12 Oct 9:30 AM"], // more than a day ago: the date
    [at(2027, 0, 4, 12, 0), "4 Jan 2027 12:00 PM"], // another year says the year
  ];
  for (const [ms, text] of cases) {
    it(`${new Date(ms).toISOString()} reads "${text}"`, () => assert.equal(formatWhen(ms, NOW), text));
  }
});

describe("taskStatus", () => {
  const task = (changes) => ({
    running: false,
    enabled: true,
    last_status: null,
    last_run_at: null,
    last_error: null,
    ...changes,
  });
  const ran = Date.UTC(2026, 9, 20, 8, 2);

  it("says a task that never ran has not run", () => {
    assert.deepEqual(taskStatus(task({}), NOW), { kind: "none", label: "Not run yet" });
  });

  it("says a task that is running is running, whatever it did last", () => {
    assert.deepEqual(taskStatus(task({ running: true, last_status: "error", last_run_at: ran }), NOW), {
      kind: "running",
      label: "Running now",
    });
  });

  it("says when a run was done", () => {
    assert.deepEqual(taskStatus(task({ last_status: "done", last_run_at: ran }), NOW), {
      kind: "done",
      label: "Done today 8:02 AM",
    });
  });

  it("gives the reason of a run that failed, was stopped, or was skipped", () => {
    assert.deepEqual(taskStatus(task({ last_status: "error", last_run_at: ran, last_error: "Gone." }), NOW), {
      kind: "error",
      label: "Failed today 8:02 AM",
      reason: "Gone.",
    });
    assert.deepEqual(taskStatus(task({ last_status: "stopped", last_run_at: ran, last_error: "Too long." }), NOW), {
      kind: "error",
      label: "Stopped today 8:02 AM",
      reason: "Too long.",
    });
    assert.deepEqual(taskStatus(task({ last_status: "skipped", last_run_at: ran, last_error: "Late." }), NOW), {
      kind: "skipped",
      label: "Skipped today 8:02 AM",
      reason: "Late.",
    });
  });

  it("explains a task the scheduler switched off with the scheduler's own words", () => {
    const off = task({
      enabled: false,
      switched_off: true,
      last_status: "error",
      last_run_at: ran,
      last_error: "Switched off after 3 failed runs in a row. The last one: Gone.",
    });
    assert.deepEqual(taskStatus(off, NOW), {
      kind: "off",
      label: "Switched off",
      reason: "Switched off after 3 failed runs in a row. The last one: Gone.",
    });
  });

  it("does not call a task that was paused by hand switched off", () => {
    assert.equal(taskStatus(task({ enabled: false, last_status: "done", last_run_at: ran }), NOW).kind, "done");
  });
});

describe("taskNext", () => {
  const next = (changes) => taskNext({ enabled: true, next_run_at: null, ...changes }, NOW);

  it("says nothing for a task that is off", () => {
    assert.equal(next({ enabled: false, next_run_at: NOW + DAY }), null);
  });

  it("says when the next run is", () => {
    assert.equal(next({ next_run_at: Date.UTC(2026, 9, 21, 8, 0) }), "Next run tomorrow 8:00 AM");
  });

  it("says a run that is due is due, since it starts within seconds or waits for a free model", () => {
    assert.equal(next({ next_run_at: NOW - 1000 }), "Due now");
    assert.equal(next({ next_run_at: NOW }), "Due now");
  });

  it("says nothing without a next run", () => {
    assert.equal(next({ next_run_at: null }), null);
  });
});
