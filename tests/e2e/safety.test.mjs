import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { chromium } from "playwright";
import { startApp } from "./harness.mjs";
import { lastReply, open, replyDone, send } from "./helpers.mjs";

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

describe("a reply full of hostile markup", () => {
  it("runs nothing, loads nothing, and shows what it was as text", async () => {
    const { page, context, requests, problems } = await open(browser, app);
    await send(page, "hostile markup please");
    await replyDone(page);
    const body = lastReply(page).locator(".body");

    assert.notEqual(await page.title(), "pwned", "a script ran");
    assert.ok(!(await page.title()).includes("pwned"));
    assert.deepEqual(
      requests.filter((url) => url.includes("evil.example")),
      [],
      "the page contacted the attacker",
    );

    const dangerous = await body.evaluate((el) => ({
      tags: [
        ...el.querySelectorAll("script, style, iframe, form, input, button, svg, img, object, embed, link, meta"),
      ].map((n) => n.tagName),
      styled: el.querySelectorAll("[style], [onerror], [onload], [onclick]").length,
      hrefs: [...el.querySelectorAll("a")].map((a) => a.getAttribute("href")),
      forged: el.querySelectorAll("[data-copy], .copy, .code, .code-head, [id], [onclick]").length,
      bad: [...el.querySelectorAll("a[href]")].filter((a) => !/^(https?:|mailto:)/.test(a.getAttribute("href"))).length,
    }));
    assert.deepEqual(dangerous.tags, []);
    assert.equal(dangerous.styled, 0);
    assert.equal(dangerous.forged, 0, "model text produced one of the UI's own controls");
    assert.equal(dangerous.bad, 0, `unsafe href: ${dangerous.hrefs}`);

    const links = await body
      .locator("a[href]")
      .evaluateAll((as) => as.map((a) => ({ href: a.href, rel: a.rel, target: a.target })));
    assert.ok(
      links.some((l) => l.href === "https://example.com/" && l.rel.includes("noopener") && l.target === "_blank"),
    );

    const text = await body.textContent();
    assert.ok(text.includes("<script>") && text.includes("<style>"), "active tags should be visible as text");
    assert.deepEqual(await problems(), [], "console errors or CSP violations");
    await context.close();
  });

  it("serves a policy that forbids inline script and remote content", async () => {
    const response = await fetch(`${app.url}/`);
    const csp = response.headers.get("content-security-policy");
    assert.match(csp, /script-src 'self'/);
    assert.match(csp, /img-src 'self'/);
    assert.doesNotMatch(csp, /unsafe-inline|unsafe-eval/);
    assert.equal(response.headers.get("x-content-type-options"), "nosniff");
  });

  it("renders markdown properly: code with a copy button, a list and a table", async () => {
    const { page, context, problems } = await open(browser, app);
    await context.grantPermissions(["clipboard-read", "clipboard-write"]);
    await send(page, "markdown please");
    await replyDone(page);
    const body = lastReply(page).locator(".body");
    await body.locator("pre code .hljs-keyword").first().waitFor({ timeout: 5000 });
    assert.equal(await body.locator("ol li").count(), 3);
    assert.equal(await body.locator("table th").count(), 2);
    assert.equal(await body.locator(".table-wrap table").count(), 1);
    await body.getByRole("button", { name: "Copy" }).click();
    assert.match(await page.evaluate(() => navigator.clipboard.readText()), /def total\(items\):/);
    assert.deepEqual(await problems(), []);
    await context.close();
  });
});
