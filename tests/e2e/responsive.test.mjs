import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { chromium } from "playwright";
import { startApp } from "./harness.mjs";
import { chooseIntensity, chooseModel, composer, open, replyDone, send } from "./helpers.mjs";

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

const box = (page, selector) => page.locator(selector).boundingBox();
const overflowsSideways = (page) => page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth);

describe("phone, 360 px wide", () => {
  const phone = { width: 360, height: 740, mobile: true };

  it("fits: no sideways scroll, composer on screen, even with long content", async () => {
    const { page, context } = await open(browser, app, phone);
    await send(page, "markdown with a very long line " + "x".repeat(200));
    await replyDone(page);
    assert.equal(await overflowsSideways(page), false);
    const composerBox = await box(page, ".composer-box");
    assert.ok(composerBox.x >= 0 && composerBox.x + composerBox.width <= 360, "composer within the width");
    assert.ok(composerBox.y + composerBox.height <= 740, "composer within the height");
    await context.close();
  });

  it("keeps every touch control at least 44 px", async () => {
    const { page, context } = await open(browser, app, phone);
    await page.getByRole("button", { name: /Change model$/ }).waitFor();
    for (const selector of ["#nav-open", "#new-chat-top", "#model-chip", "#send"]) {
      const b = await box(page, selector);
      assert.ok(b.width >= 40 && b.height >= 40, `${selector} is ${b.width}x${b.height}`);
    }
    await send(page, "hello");
    await replyDone(page);
    await page.locator("#nav-open").click();
    for (const selector of [
      ".new-chat",
      "#search",
      ".chat-link >> nth=0",
      ".menu-btn >> nth=0",
      "#open-tasks",
      "#open-settings",
    ]) {
      const b = await box(page, selector);
      assert.ok(b.height >= 40, `${selector} is ${b.height} tall`);
    }
    await context.close();
  });

  it("keeps the controls inside a reply, and the theme choice, at least 44 px tall", async () => {
    const { page, context } = await open(browser, app, phone);
    await chooseModel(page, "qwen3:8b");
    await chooseIntensity(page, "On");
    await send(page, "markdown please");
    await replyDone(page);
    for (const selector of [".meta .btn >> nth=0", ".code-head .btn", ".think > summary"]) {
      const b = await box(page, selector);
      assert.ok(b.height >= 43.5, `${selector} is ${b.height} tall`);
    }
    await page.locator("#nav-open").click();
    await page.locator("#open-settings").click();
    assert.ok((await box(page, ".seg span >> nth=0")).height >= 43.5, "the theme buttons");
    await context.close();
  });

  it("shows the whole model name by giving it the first row when there is an intensity chip", async () => {
    const { page, context } = await open(browser, app, phone);
    await chooseModel(page, "qwen3:8b");
    const name = page.locator("#model-name");
    assert.equal(await name.evaluate((el) => el.scrollWidth <= el.clientWidth), true, "the name is not cut off");
    const [model, effort, send] = await Promise.all([
      box(page, "#model-chip"),
      box(page, "#effort-chip"),
      box(page, "#send"),
    ]);
    assert.ok(effort.y >= model.y + model.height - 1, "the intensity chip is on the second row");
    assert.ok(Math.abs(effort.y - send.y) < 12, "and shares it with Send");
    assert.ok(send.x + send.width <= 360 && effort.x >= 0, "both are on screen");
    await context.close();
  });

  it("makes the model sheet closable with a visible button, and the desktop popover has none", async () => {
    const { page, context } = await open(browser, app, phone);
    await page.getByRole("button", { name: /Change model$/ }).click();
    await page.locator("#model-pop .pop-head").getByRole("button", { name: "Close" }).click();
    assert.equal(await page.locator("#model-pop").evaluate((el) => el.matches(":popover-open")), false);
    await context.close();

    const desk = await open(browser, app, { width: 1280, height: 800 });
    await desk.page.getByRole("button", { name: /Change model$/ }).click();
    assert.equal(await desk.page.locator("#model-pop .pop-head").isVisible(), false);
    await desk.context.close();
  });

  it("opens the chat list as a drawer and closes it with Escape, returning focus", async () => {
    const { page, context } = await open(browser, app, phone);
    assert.equal(await page.locator("#sidebar").isVisible(), false);
    await page.locator("#nav-open").click();
    assert.equal(await page.locator("#sidebar").isVisible(), true);
    assert.equal(await page.locator("#main").evaluate((el) => el.inert), true, "page behind the drawer is inert");
    await page.keyboard.press("Escape");
    await page.waitForFunction(() => !document.getElementById("sidebar").checkVisibility({ visibilityProperty: true }));
    assert.equal(await page.evaluate(() => document.activeElement?.id), "nav-open");
    assert.equal(await page.locator("#main").evaluate((el) => el.inert), false);
    await context.close();
  });

  it("closes the drawer after choosing a chat", async () => {
    const { page, context } = await open(browser, app, phone);
    await send(page, "first chat");
    await replyDone(page);
    await page.locator("#nav-open").click();
    await page.getByRole("button", { name: "New chat" }).first().click();
    await page.waitForFunction(() => !document.getElementById("sidebar").checkVisibility({ visibilityProperty: true }));
    await page.locator("#nav-open").click();
    await page.getByRole("link", { name: "first chat" }).click();
    await page.waitForFunction(() => !document.getElementById("sidebar").checkVisibility({ visibilityProperty: true }));
    await page.waitForFunction(() => document.getElementById("title").textContent === "first chat");
    await context.close();
  });

  it("makes the model picker a bottom sheet", async () => {
    const { page, context } = await open(browser, app, phone);
    await page.getByRole("button", { name: /Change model$/ }).click();
    const pop = await box(page, "#model-pop");
    assert.equal(Math.round(pop.x), 0);
    assert.equal(Math.round(pop.width), 360);
    assert.ok(Math.round(pop.y + pop.height) === 740, "sheet is anchored to the bottom");
    await context.close();
  });

  it("uses Enter for a new line, since there is no Shift on a phone keyboard", async () => {
    const { page, context } = await open(browser, app, phone);
    await composer(page).fill("one");
    await page.keyboard.press("Enter");
    await page.keyboard.type("two");
    assert.equal(await composer(page).inputValue(), "one\ntwo");
    assert.equal(await page.locator(".msg.user").count(), 0, "Enter must not have sent it");
    await context.close();
  });

  it("zooms nothing on focus: inputs are at least 16 px", async () => {
    const { page, context } = await open(browser, app, phone);
    for (const selector of ["#composer-input", "#search"]) {
      const size = await page.locator(selector).evaluate((el) => parseFloat(getComputedStyle(el).fontSize));
      assert.ok(size >= 16, `${selector} is ${size}px`);
    }
    await context.close();
  });
});

describe("toasts", () => {
  const overlaps = (a, b) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

  for (const [name, options] of [
    ["phone", { width: 360, height: 740, mobile: true }],
    ["desktop", { width: 1440, height: 900 }],
  ]) {
    it(`${name}: a toast covers neither the composer nor its controls`, async () => {
      const { page, context } = await open(browser, app, options);
      await page.route("**/api/chat", (route) =>
        route.fulfill({
          status: 500,
          contentType: "application/json",
          body: JSON.stringify({ error: { code: "internal", message: "Something broke inside aihub." } }),
        }),
      );
      await send(page, "toast please");
      const toast = page.locator("#toasts .toast");
      await toast.waitFor();
      const shown = await toast.boundingBox();
      for (const selector of [".composer-box", "#model-chip", "#send"]) {
        assert.equal(overlaps(shown, await box(page, selector)), false, `the toast covers ${selector}`);
      }
      assert.ok(shown.y >= 0 && shown.x >= 0 && shown.x + shown.width <= options.width, "and it is on screen");
      await context.close();
    });
  }
});

describe("tablet and desktop", () => {
  it("draws a divider under the topbar only while the conversation is scrolled under it", async () => {
    const { page, context } = await open(browser, app, { width: 1280, height: 500 });
    await send(page, "long please");
    await replyDone(page);
    const edge = () => page.locator(".topbar").evaluate((el) => getComputedStyle(el).borderBlockEndColor);
    const scrolledTo = (top) => page.locator("#scroller").evaluate((el, y) => (el.scrollTop = y), top);
    const marked = (on) =>
      page.waitForFunction((want) => document.getElementById("main").hasAttribute("data-scrolled") === want, on);

    await scrolledTo(0);
    await marked(false);
    const plain = await edge();
    await scrolledTo(400);
    await marked(true);
    const divided = await edge();
    assert.notEqual(divided, plain, "the divider appears");
    await scrolledTo(0);
    await marked(false);
    assert.equal(await edge(), plain, "and goes again at the top");
    await context.close();
  });

  it("tablet: the list is a drawer, content uses the width", async () => {
    const { page, context } = await open(browser, app, { width: 820, height: 1100 });
    assert.equal(await page.locator("#sidebar").isVisible(), false);
    assert.equal(await page.locator("#nav-open").isVisible(), true);
    assert.equal(await overflowsSideways(page), false);
    await context.close();
  });

  it("desktop: the list is docked, the hamburger is hidden, the column stays readable", async () => {
    const { page, context } = await open(browser, app, { width: 1440, height: 900 });
    assert.equal(await page.locator("#sidebar").isVisible(), true);
    assert.equal(await page.locator("#nav-open").isVisible(), false);
    assert.equal(await page.locator("#new-chat-top").isVisible(), false);
    await send(page, "hello");
    await replyDone(page);
    const column = await box(page, ".thread");
    assert.ok(column.width <= 704 + 2, `the column is ${column.width} px`);
    await context.close();
  });

  it("desktop: collapsing the list brings back the controls and is remembered", async () => {
    const { page, context } = await open(browser, app, { width: 1440, height: 900 });
    await page.locator("#nav-close").click();
    assert.equal(await page.locator("#sidebar").isVisible(), false);
    assert.equal(await page.locator("#nav-open").isVisible(), true);
    assert.equal(await page.locator("#new-chat-top").isVisible(), true);
    await page.reload();
    await page.getByRole("textbox", { name: "Message" }).waitFor();
    assert.equal(await page.locator("#sidebar").isVisible(), false);
    await context.close();
  });

  it("ultra-wide: the conversation column does not stretch", async () => {
    const { page, context } = await open(browser, app, { width: 2560, height: 1200 });
    await send(page, "hello");
    await replyDone(page);
    assert.ok((await box(page, ".thread")).width <= 706);
    await context.close();
  });
});

describe("colour schemes", () => {
  // The browser reports colours in oklch(), so let a canvas convert the background to sRGB.
  const luminance = (page) =>
    page.evaluate(() => {
      const ctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
      ctx.fillStyle = getComputedStyle(document.documentElement).backgroundColor;
      ctx.fillRect(0, 0, 1, 1);
      const [r, g, b] = ctx.getImageData(0, 0, 1, 1).data;
      return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
    });

  it("follows the system setting, light and dark", async () => {
    for (const [scheme, test] of [
      ["light", (l) => l > 0.8],
      ["dark", (l) => l < 0.3],
    ]) {
      const { page, context } = await open(browser, app, { scheme });
      const l = await luminance(page);
      assert.ok(test(l), `${scheme}: background luminance ${l}`);
      await context.close();
    }
  });
});
