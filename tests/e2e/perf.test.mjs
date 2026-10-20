// Responsiveness budgets, measured in a real browser against the real app. The numbers are printed so a
// regression is visible before it crosses a limit; the limits themselves are generous enough not to flake.

import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { chromium } from "playwright";
import { startApp } from "./harness.mjs";
import { composer, lastReply, open, replyDone, send } from "./helpers.mjs";

let app;
let browser;
before(async () => {
  app = await startApp({ seedTurns: 60 });
  browser = await chromium.launch();
});
after(async () => {
  await browser.close();
  app.stop();
});

const note = (label, value) => console.log(`    ${label}: ${value}`);

describe("loading", () => {
  it("paints early and is usable quickly", async () => {
    const { page, context } = await open(browser, app, { width: 390, height: 844, mobile: true });
    const metrics = await page.evaluate(() => ({
      fcp: performance.getEntriesByName("first-contentful-paint")[0]?.startTime,
      ready: performance.now(),
      transferred: performance.getEntriesByType("resource").reduce((sum, r) => sum + r.transferSize, 0),
      scripts: performance.getEntriesByType("resource").filter((r) => r.name.endsWith(".js") || r.name.endsWith(".mjs"))
        .length,
    }));
    note("first contentful paint", `${metrics.fcp?.toFixed(0)} ms`);
    note("interactive", `${metrics.ready.toFixed(0)} ms`);
    note("transferred", `${(metrics.transferred / 1024).toFixed(0)} KB in ${metrics.scripts} scripts`);
    assert.ok(metrics.fcp < 1000, `first contentful paint ${metrics.fcp} ms`);
    assert.ok(metrics.ready < 1500);
    assert.ok(metrics.transferred < 400 * 1024, "the first load stays small");
    await context.close();
  });
});

describe("replying", () => {
  it("shows your message and the working state within a frame or two of the tap", async () => {
    const { page, context } = await open(browser, app);
    await composer(page).fill("hello");
    const ms = await page.evaluate(
      () =>
        new Promise((resolve) => {
          const t0 = performance.now();
          const check = () =>
            document.querySelector(".msg.user") && document.querySelector(".working")
              ? resolve(performance.now() - t0)
              : requestAnimationFrame(check);
          document.getElementById("send").click();
          check();
        }),
    );
    note("tap to message and working indicator", `${ms.toFixed(0)} ms`);
    assert.ok(ms < 100, `${ms} ms`);
    await context.close();
  });

  it("streams a 2,800-token reply without long frames", async () => {
    const { page, context } = await open(browser, app, { width: 390, height: 844, mobile: true });
    await page.evaluate(() => {
      window.__frames = [];
      new PerformanceObserver((list) => list.getEntries().forEach((e) => window.__frames.push(e.duration))).observe({
        type: "long-animation-frame",
        buffered: true,
      });
    });
    await composer(page).fill("long reply please");
    const started = Date.now();
    await page.getByRole("button", { name: "Send" }).click();
    await replyDone(page);
    const elapsed = Date.now() - started;
    const frames = await page.evaluate(() => window.__frames);
    const worst = Math.max(0, ...frames);
    const blocking = frames.reduce((sum, d) => sum + Math.max(0, d - 50), 0);
    const text = await lastReply(page).locator(".body").textContent();
    note("reply", `${text.length.toLocaleString()} characters in ${elapsed} ms`);
    note("long frames", `${frames.length}, worst ${worst.toFixed(0)} ms, blocking time ${blocking.toFixed(0)} ms`);
    assert.ok(text.length > 15_000);
    assert.ok(worst < 200, `worst frame ${worst} ms`);
    assert.ok(blocking < 600, `blocking ${blocking} ms`);
    await context.close();
  });

  it("keeps typing responsive while a long reply streams", async () => {
    const { page, context } = await open(browser, app);
    await page.evaluate(() => {
      window.__events = [];
      new PerformanceObserver((list) => list.getEntries().forEach((e) => window.__events.push(e.duration))).observe({
        type: "event",
        durationThreshold: 16,
        buffered: true,
      });
    });
    await composer(page).fill("long reply please");
    await page.getByRole("button", { name: "Send" }).click();
    await composer(page).pressSequentially("typing while it streams", { delay: 20 });
    await replyDone(page);
    const slowest = Math.max(0, ...(await page.evaluate(() => window.__events)));
    note("slowest input event", `${slowest} ms`);
    assert.ok(slowest < 200, `${slowest} ms`);
    await context.close();
  });
});

describe("a long conversation", () => {
  it("is readable at once and fills in the rest while idle", async () => {
    const { page, context } = await open(browser, app, { hash: `#/c/${app.seededChat}` });
    const t0 = Date.now();
    await page.locator(".msg.assistant .meta").last().waitFor();
    const firstPaint = Date.now() - t0;
    await page.waitForFunction(() => document.querySelectorAll(".msg").length === 120, null, { timeout: 5000 });
    const complete = Date.now() - t0;
    note("newest messages visible", `${firstPaint} ms`);
    note("all 120 messages in the page", `${complete} ms`);
    assert.ok(firstPaint < 600, `${firstPaint} ms`);

    const atBottom = await page.evaluate(() => {
      const s = document.getElementById("scroller");
      return s.scrollHeight - s.scrollTop - s.clientHeight < 8;
    });
    assert.equal(atBottom, true, "it opens at the latest message, and stays there as older ones fill in");
    await context.close();
  });

  it("switches between chats quickly", async () => {
    const { page, context } = await open(browser, app, { hash: `#/c/${app.seededChat}` });
    await page.locator(".msg.assistant .meta").last().waitFor();
    await send(page, "a short one");
    await replyDone(page);
    const t0 = Date.now();
    await page.getByRole("link", { name: "A long conversation" }).click();
    await page.waitForFunction(() => document.getElementById("title").textContent === "A long conversation");
    await page.locator(".msg.assistant .meta").last().waitFor();
    const ms = Date.now() - t0;
    note("open the long chat from the list", `${ms} ms`);
    assert.ok(ms < 800, `${ms} ms`);
    await context.close();
  });
});
