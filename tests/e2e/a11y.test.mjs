import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { chromium } from "playwright";
import { startApp } from "./harness.mjs";
import { axe, composer, lastReply, open, replyDone, send } from "./helpers.mjs";

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

const focused = (page) =>
  page.evaluate(
    () => document.activeElement?.id || document.activeElement?.className || document.activeElement?.tagName,
  );

describe("axe (WCAG 2.2 AA), no violations", () => {
  const scan = async (name, options, steps = async () => {}) => {
    const { page, context } = await open(browser, app, { ...options, bypassCSP: true });
    await steps(page);
    const found = await axe(page);
    await context.close();
    assert.deepEqual(found, [], `${name}: ${found.join("\n")}`);
  };

  it("empty state", () => scan("empty", {}));
  it("a chat with markdown, code and a table", () =>
    scan("chat", {}, async (page) => {
      await send(page, "markdown please");
      await replyDone(page);
      await lastReply(page).locator("pre code .hljs-keyword").first().waitFor();
    }));
  it("an error banner", () =>
    scan("error", {}, async (page) => {
      await send(page, "error please");
      await lastReply(page).getByRole("alert").waitFor();
    }));
  it("the model picker", () =>
    scan("picker", {}, (page) => page.getByRole("button", { name: /Change model$/ }).click()));
  it("the intensity menu", () =>
    scan("intensity", {}, async (page) => {
      await page.getByRole("button", { name: /Change model$/ }).click();
      await page.getByRole("option", { name: /Haiku 5.5/ }).click();
      await page.getByRole("button", { name: /^Intensity:/ }).click();
    }));
  it("the settings dialog", () =>
    scan("settings", {}, (page) => page.getByRole("button", { name: "Settings" }).click()));
  it("the dark theme", () =>
    scan("dark", { scheme: "dark" }, async (page) => {
      await send(page, "markdown please");
      await replyDone(page);
    }));
  it("the phone drawer", () =>
    scan("drawer", { width: 360, height: 740, mobile: true }, (page) => page.locator("#nav-open").click()));
});

describe("keyboard", () => {
  it("starts with a skip link that lands in the message box", async () => {
    const { page, context } = await open(browser, app);
    const skip = page.getByRole("link", { name: "Skip to the message box" });
    assert.equal(await skip.boundingBox().then((b) => b.y < 0), true, "hidden until focused");
    await skip.focus();
    assert.equal(await skip.boundingBox().then((b) => b.y >= 0), true, "visible once focused");
    await page.keyboard.press("Enter");
    assert.equal(await focused(page), "composer-input");
    await context.close();
  });

  it("shows a visible focus ring on controls", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "hello");
    await replyDone(page);
    for (const name of [/Change model$/, "Settings", "New chat"]) {
      await page.getByRole("button", { name }).first().focus();
      await page.keyboard.press("Shift+Tab");
      await page.keyboard.press("Tab");
      const ring = await page.evaluate(() => {
        const style = getComputedStyle(document.activeElement);
        return { width: parseFloat(style.outlineWidth), style: style.outlineStyle };
      });
      assert.ok(ring.style !== "none" && ring.width >= 2, `${name}: ${JSON.stringify(ring)}`);
    }
    await context.close();
  });

  it("drives the picker with the keyboard alone, and Escape returns focus", async () => {
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: /Change model$/ }).focus();
    await page.keyboard.press("Enter");
    assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("role")), "combobox");
    const first = await page.evaluate(() => document.activeElement.getAttribute("aria-activedescendant"));
    await page.keyboard.press("ArrowDown");
    const next = await page.evaluate(() => document.activeElement.getAttribute("aria-activedescendant"));
    assert.notEqual(first, next);
    await page.keyboard.press("Escape");
    assert.equal(await page.locator("#model-pop").evaluate((el) => el.matches(":popover-open")), false);
    assert.equal(await focused(page), "model-chip");
    await context.close();
  });

  it("traps focus in the settings dialog and restores it on close", async () => {
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: "Settings" }).click();
    // The page behind a modal dialog is inert, so focus is either in the dialog or has left the page for the
    // browser's own controls (where document.activeElement is <body>). It never lands behind the dialog.
    const where = () =>
      page.evaluate(() => (document.activeElement?.closest("dialog") ? "dialog" : document.activeElement?.tagName));
    const seen = new Set();
    for (let i = 0; i < 14; i++) {
      await page.keyboard.press("Tab");
      seen.add(await where());
    }
    assert.deepEqual([...seen].sort(), ["BODY", "dialog"]);
    while ((await where()) !== "dialog") await page.keyboard.press("Tab"); // focus wraps back in
    await page.keyboard.press("Escape");
    await page.locator("dialog").waitFor({ state: "detached" });
    assert.match(await page.evaluate(() => document.activeElement?.textContent), /Settings/);
    await context.close();
  });

  it("moves through the intensity levels with the arrow keys", async () => {
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: /Change model$/ }).click();
    await page.getByRole("option", { name: /Haiku 5.5/ }).click();
    await page.getByRole("button", { name: /^Intensity:/ }).press("Enter");
    assert.match(await page.evaluate(() => document.activeElement?.textContent), /Low/);
    await page.keyboard.press("ArrowDown");
    assert.match(await page.evaluate(() => document.activeElement?.textContent), /Medium/);
    await page.keyboard.press("Enter");
    assert.match(await page.locator("#effort-name").textContent(), /Medium/);
    await context.close();
  });
});

describe("assistive technology", () => {
  it("announces when a reply starts and finishes, without reading every token", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "hello");
    await replyDone(page);
    await page.waitForFunction(() => document.getElementById("status").textContent === "Reply finished");
    assert.equal(
      await page.locator("#messages").getAttribute("aria-live"),
      null,
      "the log must not announce each token",
    );
    await context.close();
  });

  it("has landmarks and a heading outline", async () => {
    const { page, context } = await open(browser, app);
    assert.equal(await page.locator("main").count(), 1);
    assert.equal(await page.locator("aside[aria-label]").count(), 1);
    assert.equal(await page.locator("nav[aria-label]").count(), 1);
    assert.equal(await page.locator("h1").count(), 1);
    await context.close();
  });

  it("exposes the chosen model and intensity in the controls' names", async () => {
    const { page, context } = await open(browser, app);
    assert.match(await page.locator("#model-chip").getAttribute("aria-label"), /^Free Gemma 4 31B\. Change model$/);
    await page.getByRole("button", { name: /Change model$/ }).click();
    await page.getByRole("option", { name: /Haiku 5.5/ }).click();
    assert.match(await page.locator("#effort-chip").getAttribute("aria-label"), /Intensity: Low/);
    await context.close();
  });

  it("shows model-output failures to screen readers as alerts", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "error please");
    await lastReply(page).getByRole("alert").waitFor();
    await page.waitForFunction(() => /Reply failed/.test(document.getElementById("status").textContent));
    await context.close();
  });
});

describe("motion", () => {
  const animationOf = (page) =>
    page.evaluate(() => {
      const box = document.createElement("div");
      box.className = "working";
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("class", "bars");
      const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      svg.append(rect);
      box.append(svg);
      document.body.append(box);
      const name = getComputedStyle(rect).animationName;
      box.remove();
      return name;
    });

  it("animates the working bars only when motion is allowed", async () => {
    const reduced = await open(browser, app);
    assert.equal(await animationOf(reduced.page), "none");
    await reduced.context.close();

    const context = await browser.newContext({ reducedMotion: "no-preference" });
    const page = await context.newPage();
    await page.goto(app.url);
    assert.equal(await animationOf(page), "bar");
    await context.close();
  });
});
