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

describe("what a review found", () => {
  const many = async (base, count) => {
    for (let n = 1; n <= count; n++) await makeProject(base, `Many ${String(n).padStart(2, "0")}`);
  };
  const inside = (box, view) =>
    box.x >= 0 && box.y >= 0 && box.x + box.width <= view.width && box.y + box.height <= view.height;

  it("lets the Move menu reach every project, however many there are", async () => {
    const crowded = await startApp();
    try {
      await many(crowded, 18);
      const chatId = await makeChat(crowded, "a chat among many projects");
      const { page, context } = await open(browser, crowded, { hash: `#/c/${chatId}` });
      await rowMenu(page, "Options for a chat among many projects", "Move to project…");
      const menu = page.locator("#effort-pop");
      await menu.waitFor();
      const view = page.viewportSize();
      assert.ok(inside(await menu.boundingBox(), view), "the menu is on the screen");
      // The mouse wheel takes the last project into view, and it can be clicked where it is shown.
      const box = await menu.boundingBox();
      await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
      await page.mouse.wheel(0, 3000);
      await page.waitForFunction(() => {
        const rows = document.querySelector("#effort-pop [role=radiogroup]"); // the wheel is applied asynchronously
        return rows.scrollTop + rows.clientHeight >= rows.scrollHeight - 1;
      });
      const last = page.getByRole("radio", { name: "Many 18" });
      const row = await last.boundingBox();
      assert.ok(row.y >= box.y - 1 && row.y + row.height <= box.y + box.height + 1, "the last row is inside the menu");
      const hit = await page.evaluate(
        ([x, y]) => document.elementFromPoint(x, y)?.closest("[role=radio]")?.textContent,
        [row.x + row.width / 2, row.y + row.height / 2],
      );
      assert.match(hit, /Many 18/);
      await context.close();
    } finally {
      crowded.stop();
    }
  });

  it("places a popover on the side that has room for it, and caps its height there", async () => {
    const { page, context } = await open(browser, app); // 1280 x 800
    const placed = await page.evaluate(async () => {
      const { placePopover } = await import("/js/dom.js");
      const place = ({ anchorTop, rows, below }) => {
        const anchor = document.createElement("button");
        Object.assign(anchor.style, {
          position: "fixed",
          insetInlineStart: "200px",
          insetBlockStart: `${anchorTop}px`,
          blockSize: "32px",
        });
        const pop = document.createElement("div");
        pop.className = "pop";
        pop.setAttribute("popover", "manual");
        for (let n = 0; n < rows; n++) {
          const row = document.createElement("div");
          row.style.blockSize = "36px";
          pop.append(row);
        }
        document.body.append(anchor, pop);
        pop.showPopover();
        placePopover(pop, anchor, { below });
        const a = anchor.getBoundingClientRect();
        const p = pop.getBoundingClientRect();
        const result = {
          below: pop.hasAttribute("data-below"),
          gapBelow: p.top - a.bottom, // when the popover is under the anchor
          gapAbove: a.top - p.bottom, // when it is over it
          top: p.top,
          bottom: p.bottom,
          height: p.height,
        };
        pop.remove();
        anchor.remove();
        return result;
      };
      return {
        tallNearTop: place({ anchorTop: 100, rows: 14, below: false }),
        tallNearBottom: place({ anchorTop: 700, rows: 14, below: true }),
        shortAskedBelow: place({ anchorTop: 300, rows: 3, below: true }),
        shortAskedAbove: place({ anchorTop: 300, rows: 3, below: false }),
        // Neither side can show all of it: it keeps the roomier side, whichever it asked for, and fits there.
        tightAskedBelow: place({ anchorTop: 300, rows: 14, below: true }),
        tightAskedAbove: place({ anchorTop: 440, rows: 14, below: false }),
      };
    });
    const next = (gap) => gap >= 0 && gap <= 12; // next to its anchor, not merely on its side of it
    const shown = (result) => result.top >= 0 && result.bottom <= 800;
    const check = (name, result, below) => {
      assert.equal(result.below, below, `${name}: ${below ? "under" : "over"} its anchor`);
      assert.ok(
        next(below ? result.gapBelow : result.gapAbove),
        `${name}: next to its anchor ${JSON.stringify(result)}`,
      );
      assert.ok(shown(result), `${name}: on the screen ${JSON.stringify(result)}`);
    };
    check("a tall menu with no room above", placed.tallNearTop, true);
    check("a tall menu with no room below", placed.tallNearBottom, false);
    check("a short menu asked for below", placed.shortAskedBelow, true);
    check("a short menu asked for above", placed.shortAskedAbove, false);
    check("a menu too tall for either side, asked for below (more room there)", placed.tightAskedBelow, true);
    check("a menu too tall for either side, asked for above (more room there)", placed.tightAskedAbove, false);
    // 800 - (300 + 32) - 12 under the first, 440 - 12 over the second: as tall as the room, no more and no less.
    assert.ok(Math.abs(placed.tightAskedBelow.height - 456) <= 1, `${placed.tightAskedBelow.height}px of 456`);
    assert.ok(Math.abs(placed.tightAskedAbove.height - 428) <= 1, `${placed.tightAskedAbove.height}px of 428`);
    await context.close();
  });

  it("measures a popover afresh each time it is placed, not by the cap it was last given", async () => {
    const { page, context } = await open(browser, app);
    await page.setViewportSize({ width: 1280, height: 300 });
    await page.evaluate(async () => {
      const { placePopover } = await import("/js/dom.js");
      const anchor = document.createElement("button");
      anchor.id = "probe-anchor";
      Object.assign(anchor.style, {
        position: "fixed",
        insetInlineStart: "200px",
        insetBlockStart: "100px",
        blockSize: "32px",
      });
      const pop = document.createElement("div");
      pop.id = "probe-pop";
      pop.className = "pop";
      pop.setAttribute("popover", "manual");
      for (let n = 0; n < 14; n++) {
        const row = document.createElement("div");
        row.style.blockSize = "36px";
        pop.append(row);
      }
      document.body.append(anchor, pop);
      pop.showPopover();
      placePopover(pop, anchor, { below: false }); // squeezed to the little room that the short window has
    });
    await page.setViewportSize({ width: 1280, height: 800 });
    const after = await page.evaluate(async () => {
      const { placePopover } = await import("/js/dom.js");
      const anchor = document.getElementById("probe-anchor");
      anchor.style.insetBlockStart = "250px"; // 238px over it and 506px under it, for a menu that wants 480px
      const pop = document.getElementById("probe-pop");
      placePopover(pop, anchor, { below: false });
      return { below: pop.hasAttribute("data-below"), height: pop.getBoundingClientRect().height };
    });
    assert.deepEqual({ below: after.below, height: Math.round(after.height) }, { below: true, height: 480 });
    await context.close();
  });

  it("keeps both menus docked to the bottom of a phone, however high their button is", async () => {
    const quiet = await startApp(); // its own, so that the chat is near the top of the drawer
    try {
      await makeProject(quiet, "Phone one");
      const chatId = await makeChat(quiet, "a chat high on a phone");
      const { page, context } = await open(browser, quiet, {
        width: 360,
        height: 640,
        mobile: true,
        hash: `#/c/${chatId}`,
      });
      await page.locator("#nav-open").click();
      const docked = async (selector, what) => {
        const sheet = await page.locator(selector).boundingBox();
        const view = page.viewportSize();
        assert.ok(
          Math.abs(sheet.y + sheet.height - view.height) <= 1 && sheet.width === view.width,
          `${what} is docked to the bottom: ${JSON.stringify(sheet)}`,
        );
      };
      await page.getByRole("button", { name: "Options for a chat high on a phone" }).click();
      await page.locator("#row-menu").waitFor();
      await docked("#row-menu", "the row menu");
      await page.getByRole("menuitem", { name: "Move to project…" }).click();
      await page.locator("#effort-pop").waitFor();
      await docked("#effort-pop", "the Move menu");
      await context.close();
    } finally {
      quiet.stop();
    }
  });

  it("opens the Move menu under the chat's button when it fits there", async () => {
    const quiet = await startApp(); // its own, so that the chat is near the top of the sidebar
    try {
      await makeProject(quiet, "Move one");
      await makeProject(quiet, "Move two");
      const chatId = await makeChat(quiet, "a chat high in the sidebar");
      const { page, context } = await open(browser, quiet, { hash: `#/c/${chatId}` });
      await rowMenu(page, "Options for a chat high in the sidebar", "Move to project…");
      const menu = page.locator("#effort-pop");
      await menu.waitFor();
      const button = await page.getByRole("button", { name: "Options for a chat high in the sidebar" }).boundingBox();
      const box = await menu.boundingBox();
      const gap = box.y - (button.y + button.height);
      assert.ok(gap >= 0 && gap <= 12, `${gap}px under the button that led to it`);
      await context.close();
    } finally {
      quiet.stop();
    }
  });

  it("keeps a row menu next to its button, under it when there is room and over it when there is not", async () => {
    const crowded = await startApp();
    try {
      await many(crowded, 18);
      const chatId = await makeChat(crowded, "a chat at the bottom");
      const { page, context } = await open(browser, crowded, { hash: `#/c/${chatId}`, bypassCSP: true });
      // The links under the list leave a button at its end room for a menu below it. Take them away, so that the
      // list runs to the bottom of the window and there is none.
      await page.addStyleTag({ content: ".sidebar-foot { display: none; }" });
      const where = async (label) => {
        await page.getByRole("button", { name: label }).click();
        const menu = page.locator("#row-menu");
        await menu.waitFor();
        const button = await page.getByRole("button", { name: label }).boundingBox();
        const box = await menu.boundingBox();
        await page.keyboard.press("Escape");
        await menu.waitFor({ state: "hidden" });
        return { button, box };
      };
      const view = page.viewportSize();
      const top = await where("Options for Many 01");
      assert.ok(inside(top.box, view), "the menu is on the screen");
      const under = top.box.y - (top.button.y + top.button.height);
      assert.ok(under >= 0 && under <= 12, `under a button high on the screen, ${under}px from it`);
      const bottom = await where("Options for a chat at the bottom");
      assert.ok(inside(bottom.box, view), "the menu is on the screen");
      const over = bottom.button.y - (bottom.box.y + bottom.box.height);
      assert.ok(over >= 0 && over <= 12, `over a button at the bottom of the sidebar, ${over}px from it`);
      await context.close();
    } finally {
      crowded.stop();
    }
  });

  it("does the same in the sheet on a phone", async () => {
    const crowded = await startApp();
    try {
      await many(crowded, 18);
      const chatId = await makeChat(crowded, "a phone chat among many");
      const { page, context } = await open(browser, crowded, {
        width: 360,
        height: 640,
        mobile: true,
        hash: `#/c/${chatId}`,
      });
      await page.locator("#nav-open").click();
      await rowMenu(page, "Options for a phone chat among many", "Move to project…");
      const menu = page.locator("#effort-pop");
      await menu.waitFor();
      const view = page.viewportSize();
      const sheet = await menu.boundingBox();
      assert.ok(inside(sheet, view), "the sheet is on the screen");
      assert.ok(
        Math.abs(sheet.y + sheet.height - view.height) <= 1 && sheet.width === view.width,
        "docked to the bottom",
      );
      const scrolls = await page.locator("#effort-pop [role=radiogroup]").evaluate((el) => ({
        overflow: getComputedStyle(el).overflowY,
        longer: el.scrollHeight > el.clientHeight,
      }));
      assert.deepEqual(scrolls, { overflow: "auto", longer: true }, "the rows scroll");
      await context.close();
    } finally {
      crowded.stop();
    }
  });

  it("keeps the keyboard on the chat's options button after a move", async () => {
    await makeProject(app, "Keyboard move");
    const chatId = await makeChat(app, "a chat moved by keyboard");
    const { page, context } = await open(browser, app, { hash: `#/c/${chatId}` });
    await page.getByRole("button", { name: "Options for a chat moved by keyboard" }).focus();
    await page.keyboard.press("Enter");
    await page.keyboard.press("ArrowDown"); // Move to project…
    await page.keyboard.press("Enter");
    await page.getByRole("radio", { name: "Keyboard move" }).focus();
    await page.keyboard.press("Enter");
    await toast(page, "Moved to Keyboard move.").waitFor();
    assert.equal(
      await page.evaluate(() => document.activeElement?.getAttribute("aria-label")),
      "Options for a chat moved by keyboard",
    );
    await context.close();
  });

  it("puts the keyboard on New project after a project is deleted from the keyboard", async () => {
    await makeProject(app, "Keyboard delete");
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: "Options for Keyboard delete" }).focus();
    await page.keyboard.press("Enter");
    await page.keyboard.press("ArrowDown"); // Delete…
    await page.keyboard.press("Enter");
    const dlg = dialog(page, "Delete this project?");
    await dlg.waitFor();
    await dlg.getByRole("button", { name: "Delete project" }).focus();
    await page.keyboard.press("Enter");
    await toast(page, "Project deleted.").waitFor();
    assert.equal(await page.evaluate(() => document.activeElement?.id), "new-project");
    await context.close();
  });

  it("does not let a long project name widen a phone", async () => {
    const name = "W".repeat(80);
    const project = await makeProject(app, name);
    const chatId = await makeChat(app, "a chat in a long project", project.id);
    const { page, context } = await open(browser, app, {
      width: 320,
      height: 640,
      mobile: true,
      hash: `#/c/${chatId}`,
    });
    // On a phone, overflowing content makes the browser widen the layout viewport, so scrollWidth never exceeds
    // innerWidth: compare with the width the page was asked to fit.
    const sideways = () =>
      page.evaluate(
        (width) => document.documentElement.scrollWidth > width || window.innerWidth > width,
        page.viewportSize().width,
      );
    await page.locator("#title-sub").filter({ hasText: "Project: WWW" }).waitFor();
    assert.equal(await sideways(), false, "the title line");
    await page.locator("#nav-open").click();
    await page.getByRole("button", { name: `New chat in ${name}` }).click();
    await page.getByRole("heading", { name: `New chat in ${name}` }).waitFor();
    assert.equal(await sideways(), false, "the empty state");
    await page.locator("#nav-open").click();
    await rowMenu(page, `Options for ${name}`, "Delete…");
    const dlg = dialog(page, "Delete this project?");
    await dlg.waitFor();
    assert.ok(inside(await dlg.boundingBox(), page.viewportSize()), "the delete dialog fits");
    assert.equal(await sideways(), false, "with the delete dialog open");
    await context.close();
  });

  it("leaves a chat that was deleted with its project even when the page had not loaded it", async () => {
    const project = await makeProject(app, "Unlisted");
    const { page, context } = await open(browser, app); // loaded before the chat exists, so its list lacks it
    const chatId = await makeChat(app, "a chat the list does not know", project.id);
    await page.evaluate((id) => (location.hash = `#/c/${id}`), chatId);
    await page.locator("#title-sub").filter({ hasText: "Project: Unlisted" }).waitFor();
    await rowMenu(page, "Options for Unlisted", "Delete…");
    const dlg = dialog(page, "Delete this project?");
    await dlg.getByRole("checkbox", { name: "Also delete its 1 chat" }).check();
    await dlg.getByRole("button", { name: "Delete project" }).click();
    await dlg.waitFor({ state: "detached" });
    await page.getByRole("heading", { name: /^New chat on / }).waitFor();
    assert.equal((await fetch(`${app.url}/api/chats/${chatId}`)).status, 404);
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
