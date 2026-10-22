// Projects in the page: making, editing and deleting them, putting chats in them, and what the model is told.

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

const FREE = "openrouter/google/gemma-4-31b-it:free";

async function call(base, method, path, body) {
  const response = await fetch(`${base.url}${path}`, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  assert.ok(response.ok, `${method} ${path} -> ${response.status}`);
  return response.status === 204 ? null : response;
}

const makeProject = async (base, name, instructions = "") =>
  (await call(base, "POST", "/api/projects", { name, instructions })).json();

/** A chat with one exchange, made by the same API the page uses. Returns its id. */
async function makeChat(base, text, projectId) {
  const response = await call(base, "POST", "/api/chat", { model: FREE, text, project_id: projectId });
  const events = (await response.text())
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  assert.equal(events.at(-1).t, "done");
  return events[0].chat.id;
}

/** What the model was told in the latest request: the system message of the newest chat call. */
async function lastSystem() {
  const sent = (await app.requests()).filter((r) => r.body?.messages).at(-1);
  return sent.body.messages.find((m) => m.role === "system")?.content ?? "";
}

const section = (page) => page.getByRole("region", { name: "Projects" });
const toggle = (page, name) => page.getByRole("button", { name: new RegExp(`^${name}\\b`) });
const dialog = (page, name) => page.getByRole("dialog", { name });
const toast = (page, text) => page.locator(".toast").filter({ hasText: text });

async function fillProject(page, name, instructions) {
  if (name != null) await page.getByLabel("Name", { exact: true }).fill(name);
  if (instructions != null) await page.getByLabel("Instructions", { exact: true }).fill(instructions);
}

async function createProject(page, name, instructions = "") {
  await page.getByRole("button", { name: "New project" }).click();
  const dlg = dialog(page, "New project");
  await dlg.waitFor();
  await fillProject(page, name, instructions);
  await dlg.getByRole("button", { name: "Save" }).click();
  await dlg.waitFor({ state: "detached" });
}

async function rowMenu(page, label, item) {
  await page.getByRole("button", { name: label, exact: true }).click();
  await page.getByRole("menuitem", { name: item }).click();
}

describe("the Projects section", () => {
  it("is there with nothing in it, and says what projects are for", async () => {
    const bare = await startApp();
    try {
      const { page, context } = await open(browser, bare);
      await section(page).waitFor();
      assert.equal(await section(page).getByText("Group chats that share instructions.").count(), 1);
      await section(page).getByRole("button", { name: "New project" }).waitFor();
      await context.close();
    } finally {
      bare.stop();
    }
  });

  it("lists a project with its chats under it, and keeps them out of the day groups", async () => {
    const project = await makeProject(app, "Listing");
    await makeChat(app, "inside the project", project.id);
    await makeChat(app, "outside any project");
    const { page, context, problems } = await open(browser, app);
    const head = toggle(page, "Listing");
    await head.waitFor();
    assert.equal(await head.getAttribute("aria-expanded"), "false");
    assert.equal(
      await page.getByRole("link", { name: "inside the project" }).count(),
      0,
      "closed: its chats are not listed",
    );
    assert.equal(
      await page.getByRole("region", { name: "Today" }).getByRole("link", { name: "outside any project" }).count(),
      1,
    );
    await head.click();
    assert.equal(await head.getAttribute("aria-expanded"), "true");
    await page.getByRole("link", { name: "inside the project" }).waitFor();
    assert.equal(
      await page.getByRole("region", { name: "Today" }).getByRole("link", { name: "inside the project" }).count(),
      0,
    );
    assert.deepEqual(await problems(), []);
    await context.close();
  });

  it("remembers which projects are open across a reload", async () => {
    const project = await makeProject(app, "Remembered");
    await makeChat(app, "a remembered chat", project.id);
    const { page, context } = await open(browser, app);
    await toggle(page, "Remembered").click();
    await page.getByRole("link", { name: "a remembered chat" }).waitFor();
    await page.reload();
    await page.waitForFunction(() => !document.getElementById("model-chip")?.disabled);
    await page.getByRole("link", { name: "a remembered chat" }).waitFor();
    await toggle(page, "Remembered").click();
    await page.reload();
    await page.waitForFunction(() => !document.getElementById("model-chip")?.disabled);
    assert.equal(await toggle(page, "Remembered").getAttribute("aria-expanded"), "false");
    await context.close();
  });

  it("opens the project that holds the chat you go to", async () => {
    const project = await makeProject(app, "Deep link");
    const chatId = await makeChat(app, "a chat reached by its link", project.id);
    const { page, context } = await open(browser, app, { hash: `#/c/${chatId}` });
    await page.getByRole("link", { name: "a chat reached by its link" }).waitFor();
    assert.equal(await toggle(page, "Deep link").getAttribute("aria-expanded"), "true");
    await context.close();
  });

  it("shows search results as one flat list", async () => {
    const project = await makeProject(app, "Searching");
    await makeChat(app, "needle in a project", project.id);
    const { page, context } = await open(browser, app);
    await page.getByRole("searchbox", { name: "Search chats" }).fill("needle in a project");
    await page
      .getByRole("region", { name: "Results" })
      .getByRole("link", { name: /needle in a project/ })
      .waitFor();
    assert.equal(await section(page).count(), 0, "no project rows while searching");
    await context.close();
  });
});

describe("making a project", () => {
  it("takes a name and instructions, and the new project opens", async () => {
    const { page, context, problems } = await open(browser, app);
    await createProject(page, "  Made   here ", "Answer in French.");
    const head = toggle(page, "Made here");
    await head.waitFor();
    assert.equal(await head.getAttribute("aria-expanded"), "true");
    const listed = await (await call(app, "GET", "/api/projects")).json();
    const made = listed.projects.find((p) => p.name === "Made here");
    assert.equal(made.instructions, "Answer in French.");
    assert.deepEqual(await problems(), []);
    await context.close();
  });

  it("will not save without a name, and says so where the name is", async () => {
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: "New project" }).click();
    const dlg = dialog(page, "New project");
    await dlg.waitFor();
    await dlg.getByRole("button", { name: "Save" }).click();
    await dlg.getByText("Give the project a name.").waitFor();
    assert.equal(await dlg.count(), 1, "the dialog stays open");
    assert.equal(await page.evaluate(() => document.activeElement?.id), "project-name");
    await page.keyboard.press("Escape");
    await dlg.waitFor({ state: "detached" });
    await context.close();
  });

  it("limits the name and the instructions, and counts the instructions in tokens", async () => {
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: "New project" }).click();
    const dlg = dialog(page, "New project");
    await dlg.waitFor();
    assert.equal(await page.getByLabel("Name", { exact: true }).getAttribute("maxlength"), "80");
    assert.equal(await page.getByLabel("Instructions", { exact: true }).getAttribute("maxlength"), "4000");
    await fillProject(page, null, "x".repeat(300));
    await dlg.getByText(/About 101 tokens of the model's memory/).waitFor();
    await fillProject(page, null, "xx");
    await dlg.getByText(/About 1 token of the model's memory/).waitFor();
    await fillProject(page, null, "");
    assert.equal(await dlg.getByText(/About \d+ tokens?/).count(), 0, "nothing to count");
    await context.close();
  });

  it("saves from the keyboard with Enter in the name", async () => {
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: "New project" }).click();
    const dlg = dialog(page, "New project");
    await dlg.waitFor();
    await page.getByLabel("Name", { exact: true }).fill("By keyboard");
    await page.keyboard.press("Enter");
    await dlg.waitFor({ state: "detached" });
    await toggle(page, "By keyboard").waitFor();
    await context.close();
  });
});

describe("a chat in a project", () => {
  it("starts from the project, is told the project's instructions, and is listed under it", async () => {
    const project = await makeProject(app, "Trips", "Answer in French.");
    const { page, context, problems } = await open(browser, app);
    await toggle(page, "Trips").click();
    await page.getByRole("button", { name: "New chat in Trips" }).click();
    await page.getByRole("heading", { name: "New chat in Trips" }).waitFor();
    assert.match(await page.locator("#route-text").textContent(), /The project's instructions apply\.$/);
    assert.equal(await page.locator("#title-sub").textContent(), "Project: Trips");

    await page.getByRole("textbox", { name: "Message" }).fill("hello from the project");
    await page.getByRole("button", { name: "Send" }).click();
    await page.locator(".msg.assistant .meta").waitFor();
    assert.match(await lastSystem(), /Instructions for the project "Trips":\nAnswer in French\./);
    await page.getByRole("link", { name: "hello from the project" }).waitFor();
    const link = page.getByRole("region", { name: "Projects" }).getByRole("link", { name: "hello from the project" });
    assert.equal(await link.count(), 1, "listed under its project");
    const today = page.getByRole("region", { name: "Today" });
    assert.equal(await today.getByRole("link", { name: "hello from the project" }).count(), 0, "and in no day group");
    assert.equal(await page.locator("#title-sub").textContent(), "Project: Trips");
    const chats = (await (await call(app, "GET", "/api/chats")).json()).chats;
    assert.equal(chats.find((c) => c.title === "hello from the project").project_id, project.id);
    assert.deepEqual(await problems(), []);
    await context.close();
  });

  it("a chat made outside any project is told nothing about one", async () => {
    await makeProject(app, "Unused", "Never sent.");
    const { page, context } = await open(browser, app);
    await page.getByRole("textbox", { name: "Message" }).fill("a plain question");
    await page.getByRole("button", { name: "Send" }).click();
    await page.locator(".msg.assistant .meta").waitFor();
    assert.doesNotMatch(await lastSystem(), /Instructions for the project/);
    await context.close();
  });

  it("is moved into a project and out again from its menu, and the title line follows", async () => {
    const project = await makeProject(app, "Moving");
    const chatId = await makeChat(app, "a chat to move");
    const { page, context } = await open(browser, app, { hash: `#/c/${chatId}` });
    await page.getByRole("link", { name: "a chat to move" }).waitFor();
    assert.equal(await page.locator("#title-sub").isHidden(), true);

    await rowMenu(page, "Options for a chat to move", "Move to project…");
    await page.getByRole("radio", { name: "Moving" }).click();
    await toast(page, "Moved to Moving.").waitFor();
    assert.equal(await page.locator("#title-sub").textContent(), "Project: Moving");
    assert.equal(await toggle(page, "Moving").getAttribute("aria-expanded"), "true");
    await page.getByRole("region", { name: "Projects" }).getByRole("link", { name: "a chat to move" }).waitFor();
    assert.equal((await (await call(app, "GET", `/api/chats/${chatId}`)).json()).chat.project_id, project.id);

    // A reload shows it is still there, and gives a page whose menu has not just been used.
    await page.reload();
    await page.waitForFunction(() => !document.getElementById("model-chip")?.disabled);
    await page.locator("#title-sub").filter({ hasText: "Project: Moving" }).waitFor();
    await rowMenu(page, "Options for a chat to move", "Move to project…");
    assert.equal(await page.getByRole("radio", { name: "Moving" }).getAttribute("aria-checked"), "true");
    await page.getByRole("radio", { name: "No project" }).click();
    await toast(page, "Removed from Moving.").waitFor();
    assert.equal(await page.locator("#title-sub").isHidden(), true);
    await page.getByRole("region", { name: "Today" }).getByRole("link", { name: "a chat to move" }).waitFor();
    await context.close();
  });

  it("is not offered a move while there is no project to move to", async () => {
    const bare = await startApp();
    try {
      const chatId = await makeChat(bare, "a lonely chat");
      const { page, context } = await open(browser, bare, { hash: `#/c/${chatId}` });
      await page.getByRole("button", { name: "Options for a lonely chat" }).click();
      await page.getByRole("menuitem", { name: "Rename…" }).waitFor();
      assert.equal(await page.getByRole("menuitem", { name: "Move to project…" }).count(), 0);
      await context.close();
    } finally {
      bare.stop();
    }
  });
});

describe("editing and deleting a project", () => {
  it("edits its name and instructions, and the next message uses the new ones", async () => {
    const project = await makeProject(app, "Editing", "Be brief.");
    const chatId = await makeChat(app, "a chat in editing", project.id);
    const { page, context } = await open(browser, app, { hash: `#/c/${chatId}` });
    await rowMenu(page, "Options for Editing", "Edit…");
    const dlg = dialog(page, "Edit project");
    await dlg.waitFor();
    assert.equal(await page.getByLabel("Name", { exact: true }).inputValue(), "Editing");
    assert.equal(await page.getByLabel("Instructions", { exact: true }).inputValue(), "Be brief.");
    await fillProject(page, "Edited", "Be thorough.");
    await dlg.getByRole("button", { name: "Save" }).click();
    await dlg.waitFor({ state: "detached" });
    await toggle(page, "Edited").waitFor();
    assert.equal(await page.locator("#title-sub").textContent(), "Project: Edited");

    await page.getByRole("textbox", { name: "Message" }).fill("one more");
    await page.getByRole("button", { name: "Send" }).click();
    await page.locator(".msg.assistant .meta").nth(1).waitFor();
    assert.match(await lastSystem(), /Instructions for the project "Edited":\nBe thorough\./);
    await context.close();
  });

  it("keeps the chats when told to, which then show without a project", async () => {
    const project = await makeProject(app, "Keeping");
    await makeChat(app, "kept chat one", project.id);
    await makeChat(app, "kept chat two", project.id);
    const { page, context } = await open(browser, app);
    await rowMenu(page, "Options for Keeping", "Delete…");
    const dlg = dialog(page, "Delete this project?");
    await dlg.waitFor();
    assert.match(
      await dlg.textContent(),
      /“Keeping” will be deleted\. Its 2 chats stay in your list, without a project\./,
    );
    assert.equal(await dlg.getByRole("checkbox", { name: "Also delete its 2 chats" }).isChecked(), false);
    await dlg.getByRole("button", { name: "Delete project" }).click();
    await dlg.waitFor({ state: "detached" });
    await toast(page, "Project deleted.").waitFor();
    assert.equal(await toggle(page, "Keeping").count(), 0);
    const today = page.getByRole("region", { name: "Today" });
    await today.getByRole("link", { name: "kept chat one" }).waitFor();
    await today.getByRole("link", { name: "kept chat two" }).waitFor();
    await context.close();
  });

  it("deletes the chats too when asked", async () => {
    const project = await makeProject(app, "Taking");
    const doomed = await makeChat(app, "doomed chat", project.id);
    await makeChat(app, "survivor chat");
    const { page, context } = await open(browser, app);
    await rowMenu(page, "Options for Taking", "Delete…");
    const dlg = dialog(page, "Delete this project?");
    await dlg.getByRole("checkbox", { name: "Also delete its 1 chat" }).check();
    await dlg.getByRole("button", { name: "Delete project" }).click();
    await dlg.waitFor({ state: "detached" });
    await toast(page, "Project deleted.").waitFor();
    assert.equal(await page.getByRole("link", { name: "doomed chat" }).count(), 0);
    await page.getByRole("link", { name: "survivor chat" }).waitFor();
    const gone = await fetch(`${app.url}/api/chats/${doomed}`);
    assert.equal(gone.status, 404);
    await context.close();
  });

  it("leaves the chat you are in when its project and chats are deleted", async () => {
    const project = await makeProject(app, "Leaving");
    const chatId = await makeChat(app, "the chat I am in", project.id);
    const { page, context } = await open(browser, app, { hash: `#/c/${chatId}` });
    await rowMenu(page, "Options for Leaving", "Delete…");
    const dlg = dialog(page, "Delete this project?");
    await dlg.getByRole("checkbox", { name: "Also delete its 1 chat" }).check();
    await dlg.getByRole("button", { name: "Delete project" }).click();
    await dlg.waitFor({ state: "detached" });
    await page.getByRole("heading", { name: /^New chat on / }).waitFor();
    assert.match(page.url(), /#\/$|\/$/);
    await context.close();
  });

  it("offers no chat checkbox for an empty project", async () => {
    await makeProject(app, "Empty one");
    const { page, context } = await open(browser, app);
    await rowMenu(page, "Options for Empty one", "Delete…");
    const dlg = dialog(page, "Delete this project?");
    await dlg.waitFor();
    assert.match(await dlg.textContent(), /“Empty one” will be deleted\. It has no chats\./);
    assert.equal(await dlg.getByRole("checkbox").count(), 0);
    await page.keyboard.press("Escape");
    await dlg.waitFor({ state: "detached" });
    assert.equal(await toggle(page, "Empty one").count(), 1, "cancelling deletes nothing");
    await context.close();
  });
});

describe("Settings", () => {
  it("says that deleting all chats leaves the projects", async () => {
    await makeProject(app, "Stays");
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByRole("button", { name: "Delete all chats…" }).click();
    const confirm = page.getByRole("dialog", { name: "Delete all chats?" });
    await confirm.waitFor();
    assert.match(await confirm.textContent(), /Projects stay\./);
    await confirm.getByRole("button", { name: "Cancel" }).click();
    await context.close();
  });
});

describe("accessibility (axe, WCAG 2.2 AA)", () => {
  const scan = async (name, options, steps) => {
    const { page, context } = await open(browser, app, { ...options, bypassCSP: true });
    await steps(page);
    const found = await axe(page);
    await context.close();
    assert.deepEqual(found, [], `${name}: ${found.join("\n")}`);
  };

  it("the sidebar with open and closed projects", async () => {
    const project = await makeProject(app, "Axe project", "x");
    await makeChat(app, "an axe chat", project.id);
    await scan("sidebar", {}, async (page) => {
      await toggle(page, "Axe project").click();
      await page.getByRole("link", { name: "an axe chat" }).waitFor();
    });
  });
  it("the project dialog", () =>
    scan("project dialog", {}, async (page) => {
      await page.getByRole("button", { name: "New project" }).click();
      await dialog(page, "New project").waitFor();
    }));
  it("the delete dialog", async () => {
    const project = await makeProject(app, "Axe delete");
    await makeChat(app, "an axe delete chat", project.id);
    await scan("delete dialog", {}, async (page) => {
      await rowMenu(page, "Options for Axe delete", "Delete…");
      await dialog(page, "Delete this project?").waitFor();
    });
  });
  it("the move menu, in the dark theme", async () => {
    await makeProject(app, "Axe move");
    const chatId = await makeChat(app, "an axe move chat");
    await scan("move menu", { scheme: "dark", hash: `#/c/${chatId}` }, async (page) => {
      await rowMenu(page, "Options for an axe move chat", "Move to project…");
      await page.getByRole("radio", { name: "No project" }).waitFor();
    });
  });
});

describe("a phone", () => {
  for (const size of [
    { width: 320, height: 640 },
    { width: 360, height: 740 },
  ]) {
    it(`fits ${size.width} px: the drawer shows projects, the controls are 44 px, and a new chat closes it`, async () => {
      const project = await makeProject(app, `Phone ${size.width}`);
      await makeChat(app, `phone chat ${size.width}`, project.id);
      const { page, context } = await open(browser, app, { ...size, mobile: true });
      await page.locator("#nav-open").click();
      const head = toggle(page, `Phone ${size.width}`);
      await head.click();
      await page.getByRole("link", { name: `phone chat ${size.width}` }).waitFor();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth), false);
      const buttons = [
        page.getByRole("button", { name: `New chat in Phone ${size.width}` }),
        page.getByRole("button", { name: `Options for Phone ${size.width}` }),
        page.getByRole("button", { name: "New project" }),
      ];
      for (const target of [head, ...buttons]) {
        const box = await target.boundingBox();
        assert.ok(box.height >= 43.5, `a ${box.width}x${box.height} target`);
        assert.ok(box.x >= 0 && box.x + box.width <= size.width, "inside the screen");
      }
      for (const target of buttons) assert.ok((await target.boundingBox()).width >= 43.5, "wide enough to touch");
      await page.getByRole("button", { name: `New chat in Phone ${size.width}` }).click();
      await page.getByRole("heading", { name: `New chat in Phone ${size.width}` }).waitFor();
      assert.equal(await page.locator("#sidebar").isVisible(), false, "the drawer closed");
      await context.close();
    });
  }
});
