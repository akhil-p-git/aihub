// The scheduled tasks page: making, editing, running and deleting tasks, what each state says, and the chats a task makes.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { chromium } from "playwright";
import { startApp } from "./harness.mjs";
import { axe, open } from "./helpers.mjs";

let app;
let browser;
before(async () => {
  app = await startApp();
  browser = await chromium.launch();
});
after(async () => {
  await browser.close();
  app.stop();
});

const LOCAL = "ollama/qwen3:8b"; // the fake Ollama's model, with the levels Off and On
const OTHER_LOCAL = "ollama/gemma3:4b";
const FREE = "openrouter/google/gemma-4-31b-it:free";
const DAILY = { kind: "daily", time: "08:00" };

async function call(base, method, path, body) {
  const response = await fetch(`${base.url}${path}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  assert.ok(response.ok, `${method} ${path} -> ${response.status}`);
  return response.status === 204 ? null : response.json();
}

/** A chat with one exchange, made by the same streaming call the page uses. */
async function makeChat(base, text) {
  const response = await fetch(`${base.url}/api/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ model: FREE, text }),
  });
  assert.ok(response.ok, `POST /api/chat -> ${response.status}`);
  const events = (await response.text())
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  assert.equal(events.at(-1).t, "done");
}

const makeTask = (base, name, fields = {}) =>
  call(base, "POST", "/api/tasks", { name, prompt: "Say hello", model: LOCAL, schedule: DAILY, ...fields });
const tasksOf = async (base) => (await call(base, "GET", "/api/tasks")).tasks;

async function until(check, what, timeout = 20_000) {
  const deadline = Date.now() + timeout;
  for (;;) {
    const found = await check();
    if (found) return found;
    assert.ok(Date.now() < deadline, `never happened: ${what}`);
    await new Promise((r) => setTimeout(r, 100));
  }
}

/** Runs a task through the API and waits until it has finished. */
async function runAndWait(base, task) {
  await call(base, "POST", `/api/tasks/${task.id}/run`);
  return until(
    async () => (await tasksOf(base)).find((t) => t.id === task.id && !t.running && t.last_status),
    `${task.name} finished`,
  );
}

const card = (page, name) =>
  page.getByRole("listitem").filter({ has: page.getByRole("heading", { name, exact: true }) });
const dialog = (page, name) => page.getByRole("dialog", { name });
const toast = (page, text) => page.locator(".toast").filter({ hasText: text });
const sidebarLink = (page) => page.locator("#sidebar").getByRole("link", { name: "Scheduled tasks" });

async function openTasks(page) {
  await sidebarLink(page).click();
  await page.getByRole("heading", { level: 1, name: "Scheduled tasks" }).waitFor();
}

/**
 * Waits until the focus is on the element that `describe` names. A dialog that closes moves focus a moment later,
 * in its close event, and the dialog already looks gone by then, so a single read would race it.
 */
async function focusIs(page, expected, describe) {
  try {
    await page.waitForFunction(
      ([want, how]) => {
        const el = document.activeElement;
        const have = {
          id: el?.id,
          label: el?.getAttribute("aria-label"),
          role: el?.getAttribute("role"),
          text: el?.textContent?.trim(),
        };
        return have[how] === want;
      },
      [expected, describe],
      { timeout: 5000 },
    );
  } catch {
    const now = await page.evaluate(() => document.activeElement?.outerHTML.slice(0, 120));
    assert.fail(`focus never reached ${describe} "${expected}"; it is on ${now}`);
  }
}

const today = () => {
  const now = new Date();
  const month = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][now.getMonth()];
  return `${now.getDate()} ${month} ${now.getFullYear()}`;
};

/** A fresh app, for a test whose result depends on which tasks exist. */
async function withApp(fn, options) {
  const fresh = await startApp(options);
  try {
    await fn(fresh);
  } finally {
    fresh.stop();
  }
}

describe("the page", () => {
  it("is reached from the sidebar, starts empty, and puts the chat's composer away", async () => {
    await withApp(async (fresh) => {
      const { page, context, problems } = await open(browser, fresh);
      await openTasks(page);
      assert.equal(new URL(page.url()).hash, "#/tasks");
      assert.equal(await page.title(), "Scheduled tasks - aihub");
      await page.getByText("No scheduled tasks yet.").waitFor();
      await page.getByRole("button", { name: "New task" }).waitFor();
      assert.equal(await sidebarLink(page).getAttribute("aria-current"), "page");
      assert.equal(await page.getByRole("textbox", { name: "Message" }).isVisible(), false);
      assert.equal(await page.locator("#route").isVisible(), false);
      assert.deepEqual(await problems(), []);
      await context.close();
    });
  });

  it("says what a task is, where it runs and what it may not do", async () => {
    await withApp(async (fresh) => {
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      const intro = await page.locator("#tasks-page").innerText();
      assert.match(intro, /on a model on this PC/);
      assert.match(intro, /Only while aihub is open/);
      assert.match(intro, /one task at a time/);
      assert.match(intro, /15 minutes/);
      await context.close();
    });
  });

  it("opens straight from its address and goes back to the chat with the back button", async () => {
    await withApp(async (fresh) => {
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await page.getByRole("heading", { level: 1, name: "Scheduled tasks" }).waitFor();
      await page.getByRole("link", { name: "aihub, start a new chat" }).click();
      await page.getByRole("textbox", { name: "Message" }).waitFor();
      assert.equal(await page.locator("#tasks-page").isVisible(), false);
      assert.equal(await sidebarLink(page).getAttribute("aria-current"), null);
      await page.goBack();
      await page.getByRole("heading", { level: 1, name: "Scheduled tasks" }).waitFor();
      assert.equal(await page.getByRole("textbox", { name: "Message" }).isVisible(), false);
      await context.close();
    });
  });

  it("lists every task with its schedule, model, project and what happens next", async () => {
    await withApp(async (fresh) => {
      const project = await call(fresh, "POST", "/api/projects", { name: "Home", instructions: "" });
      await makeTask(fresh, "Zebra weekly", { schedule: { kind: "weekly", day: 4, time: "17:30" } });
      await makeTask(fresh, "Alpha digest", {
        schedule: { kind: "weekdays", time: "07:45" },
        project_id: project.id,
        prompt: "Summarise what I should do today.\nBe brief.",
      });
      await makeTask(fresh, "Beta hourly", { schedule: { kind: "hourly", every: 6 }, enabled: false });
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });

      const names = await page.getByRole("listitem").getByRole("heading").allInnerTexts();
      assert.deepEqual(names, ["Alpha digest", "Beta hourly", "Zebra weekly"]); // by name, like the server's list

      const alpha = card(page, "Alpha digest");
      const text = await alpha.innerText();
      assert.match(text, /Every weekday at 07:45/);
      assert.match(text, /qwen3:8b/);
      assert.match(text, /Home/);
      assert.match(text, /Not run yet/);
      assert.match(text, /Next run (today|tomorrow|[A-Z][a-z]{2}) \d\d:\d\d|Next run \d+ [A-Z][a-z]{2}/);
      assert.match(text, /Summarise what I should do today\./); // the first line of the prompt
      assert.doesNotMatch(text, /Be brief/);

      assert.match(await card(page, "Zebra weekly").innerText(), /Every Friday at 17:30/);
      const paused = card(page, "Beta hourly");
      assert.match(await paused.innerText(), /Every 6 hours/);
      assert.doesNotMatch(await paused.innerText(), /Next run/); // a task that is off has none
      assert.equal(await paused.getByRole("switch").isChecked(), false);
      assert.equal(await alpha.getByRole("switch").isChecked(), true);
      await context.close();
    });
  });
});

describe("making a task", () => {
  it("takes a name, a prompt, a model on this PC and a schedule", async () => {
    await withApp(async (fresh) => {
      const { page, context, problems } = await open(browser, fresh, { hash: "#/tasks" });
      await page.getByRole("button", { name: "New task" }).click();
      const dlg = dialog(page, "New task");
      await dlg.waitFor();

      // Only models that run on this PC are offered.
      const options = await dlg
        .getByLabel("Model")
        .locator("option")
        .evaluateAll((all) => all.map((o) => o.value));
      assert.ok(options.includes(LOCAL) && options.includes(OTHER_LOCAL), options.join());
      assert.ok(
        options.every((id) => id.startsWith("ollama/")),
        `only Ollama models: ${options.join()}`,
      );
      assert.ok(!options.includes(FREE));

      await dlg.getByLabel("Name", { exact: true }).fill("  Morning   digest ");
      await dlg.getByLabel("Prompt").fill("Summarise what I should do today.");
      await dlg.getByLabel("Model").selectOption(LOCAL);
      await dlg.getByLabel("Repeat").selectOption("weekdays");
      await dlg.getByLabel("Time").fill("07:45");
      await dlg.getByRole("button", { name: "Save" }).click();
      await dlg.waitFor({ state: "detached" });

      const made = card(page, "Morning digest");
      await made.waitFor();
      assert.match(await made.innerText(), /Every weekday at 07:45/);
      const [task] = await tasksOf(fresh);
      assert.deepEqual(
        {
          name: task.name,
          prompt: task.prompt,
          model: task.model,
          schedule: task.schedule,
          enabled: task.enabled,
          project_id: task.project_id,
        },
        {
          name: "Morning digest",
          prompt: "Summarise what I should do today.",
          model: LOCAL,
          schedule: { kind: "weekdays", time: "07:45" },
          enabled: true,
          project_id: null,
        },
      );
      assert.deepEqual(await problems(), []);
      await context.close();
    });
  });

  it("asks only for what the kind of schedule uses", async () => {
    await withApp(async (fresh) => {
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await page.getByRole("button", { name: "New task" }).click();
      const dlg = dialog(page, "New task");
      await dlg.waitFor();
      const time = dlg.getByLabel("Time");
      const day = dlg.getByLabel("Day");
      // A new task starts on every day at eight.
      assert.equal(await dlg.getByLabel("Repeat").inputValue(), "daily");
      assert.equal(await time.inputValue(), "08:00");
      assert.equal(await time.isVisible(), true);
      assert.equal(await day.isVisible(), false);
      await dlg.getByLabel("Repeat").selectOption("weekly");
      assert.equal(await day.isVisible(), true);
      assert.equal(await time.isVisible(), true);
      await dlg.getByLabel("Repeat").selectOption("hourly:3");
      assert.equal(await time.isVisible(), false);
      assert.equal(await day.isVisible(), false);
      const choices = await dlg.getByLabel("Repeat").locator("option").allInnerTexts();
      assert.deepEqual(choices, [
        "Every hour",
        "Every 2 hours",
        "Every 3 hours",
        "Every 4 hours",
        "Every 6 hours",
        "Every 8 hours",
        "Every 12 hours",
        "Every day",
        "Every weekday",
        "Every week",
      ]);
      await context.close();
    });
  });

  it("sends an hourly schedule without a time and a weekly one with its day", async () => {
    await withApp(async (fresh) => {
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      const make = async (name, fill) => {
        await page.getByRole("button", { name: "New task" }).click();
        const dlg = dialog(page, "New task");
        await dlg.waitFor();
        await dlg.getByLabel("Name", { exact: true }).fill(name);
        await dlg.getByLabel("Prompt").fill("Hi");
        await fill(dlg);
        await dlg.getByRole("button", { name: "Save" }).click();
        await dlg.waitFor({ state: "detached" });
      };
      await make("Hourly one", (dlg) => dlg.getByLabel("Repeat").selectOption("hourly:3"));
      await make("Weekly one", async (dlg) => {
        await dlg.getByLabel("Repeat").selectOption("weekly");
        await dlg.getByLabel("Day").selectOption({ label: "Sunday" });
        await dlg.getByLabel("Time").fill("21:15");
      });
      const byName = Object.fromEntries((await tasksOf(fresh)).map((t) => [t.name, t.schedule]));
      assert.deepEqual(byName["Hourly one"], { kind: "hourly", every: 3 });
      assert.deepEqual(byName["Weekly one"], { kind: "weekly", day: 6, time: "21:15" });
      await context.close();
    });
  });

  it("offers the levels of the chosen model and leaves them out for a model without any", async () => {
    await withApp(async (fresh) => {
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await page.getByRole("button", { name: "New task" }).click();
      const dlg = dialog(page, "New task");
      await dlg.waitFor();
      const effort = dlg.getByLabel("Intensity");
      await dlg.getByLabel("Model").selectOption(LOCAL);
      assert.deepEqual(await effort.locator("option").allInnerTexts(), ["Default", "Off", "On"]);
      await dlg.getByLabel("Name", { exact: true }).fill("Thoughtful");
      await dlg.getByLabel("Prompt").fill("Think about it");
      await effort.selectOption("on");
      await dlg.getByRole("button", { name: "Save" }).click();
      await dlg.waitFor({ state: "detached" });
      assert.equal((await tasksOf(fresh))[0].effort, "on");

      await page.getByRole("button", { name: "New task" }).click();
      await dlg.waitFor();
      await dlg.getByLabel("Model").selectOption(OTHER_LOCAL); // no levels
      assert.equal(await dlg.getByLabel("Intensity").isVisible(), false);
      await context.close();
    });
  });

  it("can be in a project and can start switched off", async () => {
    await withApp(async (fresh) => {
      const project = await call(fresh, "POST", "/api/projects", { name: "Home", instructions: "" });
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await page.getByRole("button", { name: "New task" }).click();
      const dlg = dialog(page, "New task");
      await dlg.waitFor();
      await dlg.getByLabel("Name", { exact: true }).fill("Chores");
      await dlg.getByLabel("Prompt").fill("List the chores");
      await dlg.getByLabel("Project").selectOption({ label: "Home" });
      await dlg.getByLabel("Run on this schedule").uncheck();
      await dlg.getByRole("button", { name: "Save" }).click();
      await dlg.waitFor({ state: "detached" });
      const [task] = await tasksOf(fresh);
      assert.equal(task.project_id, project.id);
      assert.deepEqual([task.enabled, task.next_run_at], [false, null]);
      assert.equal(await card(page, "Chores").getByRole("switch").isChecked(), false);
      await context.close();
    });
  });

  it("says what is missing and stays open", async () => {
    await withApp(async (fresh) => {
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await page.getByRole("button", { name: "New task" }).click();
      const dlg = dialog(page, "New task");
      await dlg.waitFor();
      const status = dlg.getByRole("status");
      await dlg.getByRole("button", { name: "Save" }).click();
      await status.filter({ hasText: "Give the task a name." }).waitFor();
      await focusIs(page, "task-name", "id");
      await dlg.getByLabel("Name", { exact: true }).fill("Something");
      await dlg.getByRole("button", { name: "Save" }).click();
      await status.filter({ hasText: "Write the prompt." }).waitFor();
      await focusIs(page, "task-prompt", "id");
      await dlg.getByLabel("Prompt").fill("Hi");
      await dlg.getByLabel("Time").fill("");
      await dlg.getByRole("button", { name: "Save" }).click();
      await status.filter({ hasText: "Choose a time." }).waitFor();
      assert.deepEqual(await tasksOf(fresh), []);
      await context.close();
    });
  });

  it("shows the server's reason when it refuses, and Escape closes it with focus back on New task", async () => {
    await withApp(async (fresh) => {
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await page.getByRole("button", { name: "New task" }).click();
      const dlg = dialog(page, "New task");
      await dlg.waitFor();
      await dlg.getByLabel("Name", { exact: true }).fill("Doomed");
      await dlg.getByLabel("Prompt").fill("Hi");
      // The model is removed from under the dialog: the server says so, and the dialog shows it.
      await page.route("**/api/tasks", (route) =>
        route.request().method() === "POST"
          ? route.fulfill({
              status: 400,
              contentType: "application/json",
              body: JSON.stringify({ error: { code: "unknown_model", message: "There is no model called qwen3:8b." } }),
            })
          : route.continue(),
      );
      await dlg.getByRole("button", { name: "Save" }).click();
      await dlg.getByRole("status").filter({ hasText: "There is no model called qwen3:8b." }).waitFor();
      await page.keyboard.press("Escape");
      await dlg.waitFor({ state: "detached" });
      await focusIs(page, "New task", "text");
      await context.close();
    });
  });
});

describe("running a task", () => {
  it("shows it running, then done with a link to the chat it made, and the chat is marked", async () => {
    await withApp(async (fresh) => {
      const task = await makeTask(fresh, "Morning digest", { prompt: "slow please" });
      const { page, context, problems } = await open(browser, fresh, { hash: "#/tasks" });
      const mine = card(page, "Morning digest");
      await mine.getByRole("button", { name: "Run now: Morning digest" }).click();
      await toast(page, "Started.").waitFor();
      await mine.getByText("Running now").waitFor();

      // The page keeps asking, so the end of the run shows without a reload.
      await mine.getByText(/^Done today \d\d:\d\d$/).waitFor({ timeout: 20_000 });
      assert.doesNotMatch(await mine.innerText(), /Running now/);
      const link = mine.getByRole("link", { name: "Open chat: Morning digest" });
      assert.equal(await mine.getByRole("switch").isChecked(), true);

      // The chat is in the sidebar without a reload, marked as a scheduled task's.
      const title = `Morning digest, ${today()}`;
      const row = page.locator("#chat-nav").getByRole("link", { name: new RegExp(`Scheduled task: ${title}`) });
      await row.waitFor();

      await link.click();
      await page.getByRole("heading", { level: 1, name: title }).waitFor();
      await page.locator(".msg.assistant .meta").waitFor();
      assert.match(await page.locator(".msg.user").innerText(), /slow please/);
      assert.match(await page.locator("#title-sub").innerText(), /Scheduled task: Morning digest/);
      assert.equal(await sidebarLink(page).getAttribute("aria-current"), null);
      assert.deepEqual(await problems(), []);
      assert.equal((await tasksOf(fresh))[0].id, task.id);
      await context.close();
    });
  });

  it("says why a run failed, and keeps the chat that shows how far it got", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "Flaky", { prompt: "error please" });
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      const mine = card(page, "Flaky");
      await mine.getByRole("button", { name: "Run now: Flaky" }).click();
      await mine.getByText(/^Failed today \d\d:\d\d$/).waitFor({ timeout: 20_000 });
      assert.match(await mine.innerText(), /llama runner process has terminated/);
      await mine.getByRole("link", { name: "Open chat: Flaky" }).click();
      await page.getByRole("heading", { level: 1, name: `Flaky, ${today()}` }).waitFor();
      await page.locator(".msg.assistant").waitFor();
      await context.close();
    });
  });

  it("explains a task that the scheduler switched off, and switching it on starts afresh", async () => {
    await withApp(async (fresh) => {
      const task = await makeTask(fresh, "Broken", { prompt: "error please" });
      for (let n = 0; n < 3; n++) await runAndWait(fresh, task);
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      const mine = card(page, "Broken");
      assert.match(
        await mine.innerText(),
        /Switched off after 3 failed runs in a row\. The last one: Ollama: llama runner process has terminated/,
      );
      assert.equal(await mine.getByRole("switch").isChecked(), false);
      assert.doesNotMatch(await mine.innerText(), /Next run/);
      assert.equal((await mine.innerText()).match(/Switched off/g).length, 1, "said once");

      await mine.getByRole("switch").check();
      await mine.getByText("Not run yet").waitFor();
      assert.doesNotMatch(await mine.innerText(), /Switched off/);
      assert.match(await mine.innerText(), /Next run/);
      const [after] = await tasksOf(fresh);
      assert.deepEqual([after.enabled, after.failures, after.last_status], [true, 0, null]);
      await context.close();
    });
  });

  it("does not offer the chat of an earlier run while a new run is going", async () => {
    await withApp(async (fresh) => {
      const task = await makeTask(fresh, "Twice");
      await runAndWait(fresh, task);
      await call(fresh, "PATCH", `/api/tasks/${task.id}`, { prompt: "slow please" });
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      const mine = card(page, "Twice");
      const link = mine.getByRole("link", { name: "Open chat: Twice" });
      await link.waitFor(); // the first run's chat
      await mine.getByRole("button", { name: "Run now: Twice" }).click();
      await mine.getByText("Running now").waitFor();
      assert.equal(await link.count(), 0);
      await mine.getByText(/^Done today/).waitFor({ timeout: 20_000 });
      assert.equal(await link.count(), 1);
      await context.close();
    });
  });

  it("refuses a second run while one is going, and says so", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "First", { prompt: "slow please" });
      await makeTask(fresh, "Second", { prompt: "Hi" });
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await card(page, "First").getByRole("button", { name: "Run now: First" }).click();
      await card(page, "First").getByText("Running now").waitFor();
      await card(page, "Second").getByRole("button", { name: "Run now: Second" }).click();
      await toast(page, "Another scheduled task is running.").waitFor();
      assert.equal((await tasksOf(fresh)).find((t) => t.name === "Second").last_status, null);
      await card(page, "First").getByText(/^Done/).waitFor({ timeout: 20_000 });
      await context.close();
    });
  });

  it("says why a task cannot run at all", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "Gone model", { model: LOCAL });
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await page.route("**/api/tasks/*/run", (route) =>
        route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            error: { code: "cannot_run", message: "qwen3:8b isn't available: Ollama isn't running." },
          }),
        }),
      );
      await card(page, "Gone model").getByRole("button", { name: "Run now: Gone model" }).click();
      await toast(page, "qwen3:8b isn't available: Ollama isn't running.").waitFor();
      await context.close();
    });
  });
});

describe("changing a task", () => {
  it("leaves the cards alone when a refresh brings nothing new, and rebuilds them when it does", async () => {
    await withApp(async (fresh) => {
      const task = await makeTask(fresh, "Steady");
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await card(page, "Steady").waitFor();
      const refreshed = () =>
        page.waitForResponse((r) => r.url().endsWith("/api/tasks") && r.request().method() === "GET");
      const mark = () => page.evaluate(() => (document.querySelector(".task").dataset.probe = "same"));
      const marked = () => page.evaluate(() => document.querySelector(".task")?.dataset.probe ?? null);

      await mark();
      const first = refreshed();
      await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      await first;
      await page.waitForTimeout(150); // the page had its chance to redraw
      assert.equal(await marked(), "same", "nothing changed, so the card is the same one");

      await call(fresh, "PATCH", `/api/tasks/${task.id}`, { name: "Steady, renamed" });
      const second = refreshed();
      await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      await second;
      await card(page, "Steady, renamed").waitFor();
      assert.equal(await marked(), null, "something changed, so the cards were rebuilt");
      await context.close();
    });
  });

  it("does not let an older answer replace a newer one", async () => {
    await withApp(async (fresh) => {
      const task = await makeTask(fresh, "Original name");
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await card(page, "Original name").waitFor();
      let hold = false;
      let captured;
      const answered = new Promise((resolve) => (captured = resolve));
      await page.route("**/api/tasks", async (route) => {
        if (route.request().method() !== "GET" || !hold) return route.continue();
        hold = false; // only the first one
        const answer = await route.fetch(); // the list as it is now, with the old name
        captured();
        await new Promise((resolve) => setTimeout(resolve, 900)); // delivered late
        await route.fulfill({ response: answer });
      });
      hold = true;
      await page.evaluate(() => window.dispatchEvent(new Event("focus"))); // the request that is held back
      await answered; // the old answer exists, and the rename has not happened yet
      await call(fresh, "PATCH", `/api/tasks/${task.id}`, { name: "Newer name" });
      await page.evaluate(() => window.dispatchEvent(new Event("focus"))); // a later one, answered at once
      await card(page, "Newer name").waitFor();
      await page.waitForTimeout(1500); // the held one arrives
      assert.equal(await card(page, "Original name").count(), 0, "the old answer was not drawn over the new one");
      assert.equal(await card(page, "Newer name").count(), 1);
      await context.close();
    });
  });

  it("tells a screen reader when a run ends", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "Announced", { prompt: "slow please" });
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await card(page, "Announced").getByRole("button", { name: "Run now: Announced" }).click();
      await card(page, "Announced").getByText("Running now").waitFor();
      await page
        .locator("#status")
        .filter({ hasText: /^Announced: Done today \d\d:\d\d$/ })
        .waitFor({ timeout: 20_000 });
      await context.close();
    });
  });

  it("says so when no model on this PC was found", async () => {
    await withApp(async (fresh) => {
      const { page, context } = await open(browser, fresh);
      await page.route("**/api/bootstrap", async (route) => {
        const response = await route.fetch();
        const json = await response.json();
        json.models = json.models.filter((m) => m.provider !== "ollama");
        await route.fulfill({ response, json });
      });
      await page.reload();
      await openTasks(page);
      await page.getByRole("button", { name: "New task" }).click();
      const dlg = dialog(page, "New task");
      await dlg.waitFor();
      await dlg.getByText("No model on this PC was found. Start Ollama and reload the page.").waitFor();
      await dlg.getByLabel("Name", { exact: true }).fill("No model");
      await dlg.getByLabel("Prompt").fill("Hi");
      await dlg.getByRole("button", { name: "Save" }).click();
      await dlg.getByRole("status").filter({ hasText: "Choose a model." }).waitFor();
      assert.deepEqual(await tasksOf(fresh), []);
      await context.close();
    });
  });

  it("switches it off and on, and the next run follows", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "Daily thing");
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      const mine = card(page, "Daily thing");
      const switcher = mine.getByRole("switch");
      assert.match(await mine.innerText(), /Next run/);

      await switcher.uncheck();
      await until(async () => (await tasksOf(fresh))[0].enabled === false, "task off");
      await mine.getByText(/Next run/).waitFor({ state: "detached" }); // the page asks again, then redraws
      assert.equal((await tasksOf(fresh))[0].next_run_at, null);

      await switcher.check();
      await until(async () => (await tasksOf(fresh))[0].enabled === true, "task on");
      await mine.getByText(/Next run/).waitFor();
      await context.close();
    });
  });

  it("puts the switch back and says so when the server refuses", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "Stubborn");
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      await page.route("**/api/tasks/*", (route) =>
        route.request().method() === "PATCH"
          ? route.fulfill({
              status: 500,
              contentType: "application/json",
              body: JSON.stringify({ error: { code: "internal", message: "The server hit an unexpected error." } }),
            })
          : route.continue(),
      );
      const switcher = card(page, "Stubborn").getByRole("switch");
      await switcher.uncheck();
      await toast(page, "The server hit an unexpected error.").waitFor();
      await until(async () => (await switcher.isChecked()) === true, "switch back on");
      await context.close();
    });
  });

  it("opens the editor with what the task has, saves changes, and puts focus back on Edit", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "Original", {
        prompt: "Original prompt",
        schedule: { kind: "weekly", day: 2, time: "09:30" },
        effort: "on",
      });
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      const edit = card(page, "Original").getByRole("button", { name: "Edit: Original" });
      await edit.click();
      const dlg = dialog(page, "Edit task");
      await dlg.waitFor();
      assert.equal(await dlg.getByLabel("Name", { exact: true }).inputValue(), "Original");
      assert.equal(await dlg.getByLabel("Prompt").inputValue(), "Original prompt");
      assert.equal(await dlg.getByLabel("Model").inputValue(), LOCAL);
      assert.equal(await dlg.getByLabel("Intensity").inputValue(), "on");
      assert.equal(await dlg.getByLabel("Repeat").inputValue(), "weekly");
      assert.equal(await dlg.getByLabel("Day").inputValue(), "2");
      assert.equal(await dlg.getByLabel("Time").inputValue(), "09:30");
      assert.equal(await dlg.getByLabel("Run on this schedule").isChecked(), true);

      await dlg.getByLabel("Name", { exact: true }).fill("Renamed");
      await dlg.getByLabel("Repeat").selectOption("daily");
      await dlg.getByLabel("Time").fill("06:05");
      await dlg.getByRole("button", { name: "Save" }).click();
      await dlg.waitFor({ state: "detached" });

      const renamed = card(page, "Renamed");
      await renamed.waitFor();
      assert.match(await renamed.innerText(), /Every day at 06:05/);
      await focusIs(page, "Edit: Renamed", "label");
      const [task] = await tasksOf(fresh);
      assert.deepEqual([task.name, task.schedule, task.effort], ["Renamed", { kind: "daily", time: "06:05" }, "on"]);
      await context.close();
    });
  });

  it("sends only what was changed, and nothing when nothing was", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "Careful", { effort: "on", schedule: { kind: "weekly", day: 1, time: "10:00" } });
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      const sent = [];
      page.on("request", (r) => r.method() === "PATCH" && sent.push(JSON.parse(r.postData())));
      const edit = (name) => card(page, name).getByRole("button", { name: `Edit: ${name}` });

      await edit("Careful").click();
      const dlg = dialog(page, "Edit task");
      await dlg.waitFor();
      await dlg.getByLabel("Name", { exact: true }).fill("Renamed");
      await dlg.getByRole("button", { name: "Save" }).click();
      await card(page, "Renamed").waitFor();
      assert.deepEqual(sent, [{ name: "Renamed" }]); // the schedule is not sent, so its next run is not moved

      sent.length = 0;
      await edit("Renamed").click();
      await dlg.waitFor();
      await dlg.getByLabel("Repeat").selectOption("daily");
      await dlg.getByLabel("Time").fill("06:00");
      await dlg.getByRole("button", { name: "Save" }).click();
      await card(page, "Renamed").getByText("Every day at 06:00").waitFor();
      assert.deepEqual(sent, [{ schedule: { kind: "daily", time: "06:00" } }]);

      sent.length = 0;
      await edit("Renamed").click();
      await dlg.waitFor();
      await dlg.getByRole("button", { name: "Save" }).click();
      await dlg.waitFor({ state: "detached" });
      assert.deepEqual(sent, []); // nothing changed, so nothing was asked of the server
      await context.close();
    });
  });

  it("keeps a model that is no longer installed instead of quietly changing it", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "Orphan", { effort: "on" });
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      // The model list the page holds no longer has the task's model, as after Ollama drops it.
      await page.route("**/api/bootstrap", async (route) => {
        const response = await route.fetch();
        const json = await response.json();
        json.models = json.models.filter((m) => m.id !== LOCAL);
        await route.fulfill({ response, json });
      });
      await page.reload();
      await card(page, "Orphan").getByRole("button", { name: "Edit: Orphan" }).click();
      const dlg = dialog(page, "Edit task");
      await dlg.waitFor();
      assert.equal(await dlg.getByLabel("Model").inputValue(), LOCAL);
      assert.match(await dlg.getByLabel("Model").locator("option:checked").innerText(), /not installed/);
      await dlg.getByLabel("Name", { exact: true }).fill("Orphan renamed");
      await dlg.getByRole("button", { name: "Save" }).click();
      await card(page, "Orphan renamed").waitFor();
      const [kept] = await tasksOf(fresh);
      assert.deepEqual([kept.model, kept.effort], [LOCAL, "on"]); // neither was swapped for another
      await context.close();
    });
  });

  it("asks before deleting, keeps the chats it made, and moves focus to New task", async () => {
    await withApp(async (fresh) => {
      const task = await makeTask(fresh, "Short lived");
      await runAndWait(fresh, task);
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      const mine = card(page, "Short lived");
      await mine.getByRole("button", { name: "Delete: Short lived" }).click();
      const dlg = dialog(page, "Delete this task?");
      await dlg.waitFor();
      assert.match(await dlg.innerText(), /Its chats stay in your list\./);
      await dlg.getByRole("button", { name: "Cancel" }).click();
      await dlg.waitFor({ state: "detached" });
      assert.equal((await tasksOf(fresh)).length, 1);

      await mine.getByRole("button", { name: "Delete: Short lived" }).click();
      await dlg.waitFor();
      await dlg.getByRole("button", { name: "Delete task" }).click();
      await mine.waitFor({ state: "detached" });
      await toast(page, "Task deleted.").waitFor();
      await page.getByText("No scheduled tasks yet.").waitFor();
      await focusIs(page, "New task", "text");
      assert.deepEqual(await tasksOf(fresh), []);

      // The chat it made is still there, but no longer marked.
      const row = page.locator("#chat-nav").getByRole("link", { name: `Short lived, ${today()}` });
      await row.waitFor();
      assert.doesNotMatch(await row.innerText(), /Scheduled task/);
      await context.close();
    });
  });
});

describe("the rest of the page", () => {
  it("marks the chats a task made in the sidebar, and only those", async () => {
    await withApp(async (fresh) => {
      const task = await makeTask(fresh, "Marked");
      await runAndWait(fresh, task);
      await makeChat(fresh, "An ordinary chat");
      const { page, context } = await open(browser, fresh);
      const links = page.locator("#chat-nav .chat-link");
      await links.filter({ hasText: "An ordinary chat" }).waitFor();
      assert.equal(await links.filter({ hasText: "An ordinary chat" }).locator(".task-mark").count(), 0);
      const marked = links.filter({ hasText: `Marked, ${today()}` });
      assert.equal(await marked.locator(".task-mark").count(), 1);
      assert.match(await marked.innerText(), /Scheduled task/);
      await context.close();
    });
  });

  it("says in Delete all chats that tasks stay", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "Stays");
      const { page, context } = await open(browser, fresh);
      await page.getByRole("button", { name: "Settings" }).click();
      await page.getByRole("button", { name: "Delete all chats…" }).click();
      const dlg = dialog(page, "Delete all chats?");
      await dlg.waitFor();
      assert.match(await dlg.innerText(), /Scheduled tasks stay\./);
      assert.doesNotMatch(await dlg.innerText(), /Projects/);
      await dlg.getByRole("button", { name: "Cancel" }).click();
      await context.close();
    });
  });

  it("says projects and tasks stay when both exist", async () => {
    await withApp(async (fresh) => {
      await call(fresh, "POST", "/api/projects", { name: "Home", instructions: "" });
      await makeTask(fresh, "Stays");
      const { page, context } = await open(browser, fresh);
      await page.getByRole("button", { name: "Settings" }).click();
      await page.getByRole("button", { name: "Delete all chats…" }).click();
      const dlg = dialog(page, "Delete all chats?");
      await dlg.waitFor();
      assert.match(await dlg.innerText(), /Projects and scheduled tasks stay\./);
      await context.close();
    });
  });

  it("notices a chat that a task made while the window was away, when it comes back", async () => {
    await withApp(async (fresh) => {
      const task = await makeTask(fresh, "While away");
      const { page, context } = await open(browser, fresh);
      await runAndWait(fresh, task); // made by the server, not by this page
      await page.evaluate(() => window.dispatchEvent(new Event("focus")));
      await page
        .locator("#chat-nav")
        .getByRole("link", { name: new RegExp(`While away, ${today()}`) })
        .waitFor();
      await context.close();
    });
  });
});

describe("on a phone", () => {
  for (const [width, height] of [
    [360, 740],
    [320, 640],
  ]) {
    it(`fits at ${width} px with a long name, and every control can be touched`, async () => {
      await withApp(async (fresh) => {
        const name = "A".repeat(60);
        await makeTask(fresh, name, { prompt: "word ".repeat(200) });
        const { page, context } = await open(browser, fresh, { width, height, mobile: true });
        await page.locator("#nav-open").click();
        await sidebarLink(page).click();
        await page.getByRole("heading", { level: 1, name: "Scheduled tasks" }).waitFor();
        const sideways = () =>
          page.evaluate((w) => document.documentElement.scrollWidth > w || window.innerWidth > w, width);
        assert.equal(await sideways(), false, "the page");

        const mine = card(page, name);
        await mine.waitFor();
        for (const control of [
          page.getByRole("button", { name: "New task" }),
          mine.getByRole("button", { name: `Run now: ${name}` }),
          mine.getByRole("button", { name: `Edit: ${name}` }),
          mine.getByRole("button", { name: `Delete: ${name}` }),
          mine.getByRole("switch"),
        ]) {
          const box = await control.boundingBox();
          assert.ok(box.height >= 43.5 && box.width >= 43.5, `${JSON.stringify(box)} is big enough to touch`);
        }
        const bounds = await mine.boundingBox();
        assert.ok(bounds.x >= 0 && bounds.x + bounds.width <= width, "the card is inside the screen");

        await mine.getByRole("button", { name: `Edit: ${name}` }).click();
        const dlg = dialog(page, "Edit task");
        await dlg.waitFor();
        const box = await dlg.boundingBox();
        assert.ok(box.x >= 0 && box.x + box.width <= width, "the editor fits");
        assert.equal(await sideways(), false, "with the editor open");
        await context.close();
      });
    });
  }
});

describe("accessibility", () => {
  it("has no violations on the empty page, the list in each state, and the dialogs", async () => {
    await withApp(async (fresh) => {
      const empty = await open(browser, fresh, { hash: "#/tasks", bypassCSP: true });
      assert.deepEqual(await axe(empty.page), [], "the empty page");
      await empty.context.close();

      const done = await makeTask(fresh, "Done one");
      await runAndWait(fresh, done);
      const failed = await makeTask(fresh, "Failed one", { prompt: "error please" });
      await runAndWait(fresh, failed);
      const broken = await makeTask(fresh, "Broken one", { prompt: "error please" });
      for (let n = 0; n < 3; n++) await runAndWait(fresh, broken);
      await makeTask(fresh, "Paused one", { enabled: false });
      const running = await makeTask(fresh, "Running one", { prompt: "slow please" });

      for (const scheme of ["light", "dark"]) {
        await call(fresh, "POST", `/api/tasks/${running.id}/run`);
        const { page, context } = await open(browser, fresh, { hash: "#/tasks", scheme, bypassCSP: true });
        await card(page, "Running one").getByText("Running now").waitFor();
        assert.deepEqual(await axe(page), [], `the list, ${scheme}`);

        await page.getByRole("button", { name: "New task" }).click();
        await dialog(page, "New task").waitFor();
        assert.deepEqual(await axe(page), [], `the new task dialog, ${scheme}`);
        await dialog(page, "New task").getByLabel("Repeat").selectOption("weekly");
        assert.deepEqual(await axe(page), [], `with a weekly schedule, ${scheme}`);
        await page.keyboard.press("Escape");

        await card(page, "Done one").getByRole("button", { name: "Delete: Done one" }).click();
        await dialog(page, "Delete this task?").waitFor();
        assert.deepEqual(await axe(page), [], `the delete dialog, ${scheme}`);
        await context.close();
        // The running task finishes before the next pass.
        await until(async () => !(await tasksOf(fresh)).some((t) => t.running), "the run ends");
      }
    });
  });

  it("can be worked with the keyboard alone", async () => {
    await withApp(async (fresh) => {
      await makeTask(fresh, "By keyboard");
      const { page, context } = await open(browser, fresh, { hash: "#/tasks" });
      const mine = card(page, "By keyboard");
      await mine.getByRole("switch").focus();
      await page.keyboard.press("Space"); // off
      await until(async () => (await tasksOf(fresh))[0].enabled === false, "switched off by key");
      await page.keyboard.press("Space"); // on again
      await until(async () => (await tasksOf(fresh))[0].enabled === true, "switched on by key");
      await focusIs(page, "switch", "role");

      await mine.getByRole("button", { name: "Edit: By keyboard" }).focus();
      await page.keyboard.press("Enter");
      const dlg = dialog(page, "Edit task");
      await dlg.waitFor();
      await focusIs(page, "task-name", "id");
      await page.keyboard.press("Escape");
      await dlg.waitFor({ state: "detached" });
      await focusIs(page, "Edit: By keyboard", "label");
      await context.close();
    });
  });
});
