// The image UI against a fake engine: choosing an image model, making an image, stopping it, leaving and coming back,
// failing, making it again, and how all of that reads to a screen reader and fits on a phone.

import assert from "node:assert/strict";
import net from "node:net";
import { after, afterEach, before, describe, it } from "node:test";
import { chromium } from "playwright";
import { startApp } from "./harness.mjs";
import { axe, chooseModel, lastReply, open } from "./helpers.mjs";

let app;
let browser;
before(async () => {
  app = await startApp({ images: true });
  browser = await chromium.launch();
});
after(async () => {
  await browser.close();
  app.stop();
});
// A test that fails with an image held would leave the GPU taken, and every later test would wait for it.
afterEach(() => app.engine.release(100));

const imagePrompt = (page) => page.getByRole("textbox", { name: "Image prompt" });
const createButton = (page) => page.getByRole("button", { name: "Create image" });
// Exact, because the sidebar has "Options for <title>" buttons, and a title can say "stop".
const stopButton = (page) => page.getByRole("button", { name: "Stop", exact: true });
const shapeChip = (page) => page.locator("#shape-chip");
const slate = (page) => page.locator(".img-slate");
const label = (page) => page.locator(".img-label");
const images = (page) => page.locator(".msg.assistant .img-out");
const status = (page) => page.locator("#status");

async function chooseImageModel(page, name = "Z-Image Turbo") {
  await page.getByRole("button", { name: /Change model$/ }).click();
  await page.getByRole("option", { name: new RegExp(name) }).click();
}

async function chooseShape(page, shape) {
  await shapeChip(page).click();
  await page.getByRole("radio", { name: shape, exact: true }).click();
}

async function make(page, prompt) {
  await imagePrompt(page).fill(prompt);
  await createButton(page).click();
}

/** Waits until the nth image (from 1) is on the page and decoded. */
async function imageShown(page, n = 1) {
  await page.waitForFunction(
    (count) => {
      const shown = [...document.querySelectorAll(".msg.assistant .img-out")];
      return shown.length >= count && shown.every((img) => img.complete && img.naturalWidth > 0);
    },
    n,
    { timeout: 30_000 },
  );
}

/** A chat whose first image is already made, by the same API the page uses. Returns the chat's id. */
async function imageChat(text) {
  const response = await fetch(`${app.url}/api/chat`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: null, model: "images/zimage", effort: null, text, shape: "square" }),
  });
  const events = (await response.text())
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  assert.equal(events.at(-1).t, "done", "the image was made");
  return events[0].chat.id;
}

/** A TCP relay in front of the app that can cut every connection, and refuse new ones, as a network failure would. */
async function relay(target) {
  const sockets = new Set();
  const control = { url: "", down: false };
  const server = net.createServer((client) => {
    if (control.down) return client.destroy();
    const upstream = net.connect(new URL(target).port, "127.0.0.1");
    for (const [from, to] of [
      [client, upstream],
      [upstream, client],
    ]) {
      sockets.add(from);
      from.on("error", () => from.destroy());
      from.on("close", () => {
        sockets.delete(from);
        to.destroy();
      });
      from.pipe(to);
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  control.url = `http://127.0.0.1:${server.address().port}`;
  control.cut = () => sockets.forEach((socket) => socket.destroy());
  control.close = () => {
    control.cut();
    server.close();
  };
  return control;
}

const announced = (page, pattern) =>
  page.waitForFunction(
    ([source]) => new RegExp(source).test(document.getElementById("status").textContent),
    [pattern.source],
  );

describe("image mode", () => {
  it("lists the image models in a group of their own, with what each is good at", async () => {
    const { page, context, problems } = await open(browser, app);
    await page.getByRole("button", { name: /Change model$/ }).click();
    const group = page.getByRole("group", { name: "Makes images" });
    await group.waitFor();
    const names = await group.getByRole("option").locator(".opt-name").allTextContents();
    assert.deepEqual(names, ["Z-Image Turbo", "FLUX.2 klein 4B"]);
    const text = await group.textContent();
    assert.match(text, /Best with text in images\./);
    assert.match(text, /Fast drafts; edits images; text often misspelled\./);
    assert.deepEqual(await problems(), []);
    await context.close();
  });

  it("turns the composer into an image prompt, with a Shape chip instead of an intensity chip", async () => {
    const { page, context, problems } = await open(browser, app);
    await chooseImageModel(page);
    assert.equal(await imagePrompt(page).getAttribute("placeholder"), "Describe an image…");
    await createButton(page).waitFor();
    assert.equal(await shapeChip(page).isVisible(), true);
    assert.equal(await page.locator("#effort-chip").isHidden(), true);
    assert.equal(await shapeChip(page).getAttribute("aria-label"), "Shape: Square, about 23 s. Change shape");
    await page.getByRole("heading", { name: "Image mode on Z-Image Turbo" }).waitFor();
    const route = await page.locator("#route-text").textContent();
    assert.match(route, /Nothing leaves the machine; local chat models pause while it works\./);

    // The shape menu names what each shape costs in time, and the choice shows on the chip.
    await shapeChip(page).click();
    const note = await page.locator("#effort-pop .pop-note").textContent();
    assert.equal(note, "Square, about 23 s. Wide, about 12 s. Tall, about 12 s.");
    await page.getByRole("radio", { name: "Wide", exact: true }).click();
    assert.equal(await shapeChip(page).getAttribute("aria-label"), "Shape: Wide, about 12 s. Change shape");
    assert.equal(await page.locator("#shape-name").textContent(), "Wide");
    assert.deepEqual(await problems(), []);
    await context.close();
  });

  it("leaves image mode when a text model is chosen, and opens a chat made with an image model in image mode", async () => {
    const chatId = await imageChat("a seeded lamp");
    const { page, context } = await open(browser, app, { hash: `#/c/${chatId}` });
    await createButton(page).waitFor();
    await announced(page, /^Opened a seeded lamp\. Image mode\.$/);
    await chooseModel(page, "qwen3:8b");
    await page.getByRole("textbox", { name: "Message" }).waitFor();
    assert.equal(await shapeChip(page).isHidden(), true);
    await context.close();
  });

  it("shows an Images row in Settings only where images are set up", async () => {
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: "Settings" }).click();
    const dialog = page.getByRole("dialog");
    await dialog.waitFor();
    assert.match(await dialog.textContent(), /Images\s*Ready, 2 models/);
    await context.close();
  });
});

describe("making an image", () => {
  it("shows the image, what made it and what to do next, and says so out loud", async () => {
    const { page, context, problems } = await open(browser, app);
    await chooseImageModel(page);
    await chooseShape(page, "Wide");
    await make(page, "a lighthouse at dusk");

    await slate(page).waitFor();
    assert.equal(await slate(page).getAttribute("role"), "progressbar");
    assert.equal(await slate(page).getAttribute("aria-live"), "off");
    assert.equal(await label(page).getAttribute("aria-hidden"), "true");
    await announced(page, /^Creating image, about 12 seconds$/);

    await imageShown(page);
    const reply = lastReply(page);
    const img = reply.locator(".img-out");
    assert.equal(await img.getAttribute("alt"), "Generated image: a lighthouse at dusk");
    assert.equal(await slate(page).count(), 0, "the slate is gone");

    const footer = await reply.locator(".meta").textContent();
    for (const part of [/Z-Image Turbo/, /\d+ s/, /8 steps/, /seed \d+/, /1024 by 576/, /Free, on this PC/]) {
      assert.match(footer, part);
    }
    const download = reply.getByRole("link", { name: "Download image, 1024 by 576" });
    assert.match(await download.getAttribute("download"), /^a-lighthouse-at-dusk-\d+-wide\.webp$/);
    assert.match(await download.getAttribute("href"), /^\/api\/chats\/[0-9a-f]{16}\/images\/[0-9a-f]{16}$/);
    for (const name of ["New seed", "Reuse prompt", "Redo with FLUX.2 klein 4B", "Full size"]) {
      await reply.getByRole("button", { name }).waitFor();
    }
    assert.equal(await reply.getByRole("button", { name: "Retry reply" }).count(), 0, "Retry is for failed runs");
    await announced(page, /^Image ready\. 1024 by 576, \d+ seconds\.$/);

    const body = (await app.engine.submitted()).at(-1);
    assert.equal(body.prompt, "a lighthouse at dusk");
    assert.deepEqual([body.width, body.height], [1024, 576]);

    // 'Full size' lifts the height cap, and says that it is on.
    const full = reply.getByRole("button", { name: "Full size" });
    await full.click();
    assert.equal(await full.getAttribute("aria-pressed"), "true");
    assert.equal(await reply.locator("figure[data-full]").count(), 1);
    assert.deepEqual(await problems(), []);
    await context.close();
  });

  it("serves the image from storage after a reload, with the same alt text", async () => {
    const { page, context } = await open(browser, app);
    await chooseImageModel(page);
    await make(page, "a paper boat");
    await imageShown(page);
    await page.reload();
    await page.waitForFunction(() => !document.getElementById("model-chip")?.disabled);
    await imageShown(page);
    assert.equal(await images(page).first().getAttribute("alt"), "Generated image: a paper boat");
    const href = await lastReply(page)
      .getByRole("link", { name: /^Download image/ })
      .getAttribute("href");
    const served = await page.request.get(`${app.url}${href}`);
    assert.equal(served.headers()["content-type"], "image/webp");
    await context.close();
  });

  it("makes the same prompt again as a new turn, on this or the other model", async () => {
    const { page, context } = await open(browser, app);
    await chooseImageModel(page);
    await make(page, "a red kite");
    await imageShown(page, 1);

    await lastReply(page).getByRole("button", { name: "New seed" }).click();
    await imageShown(page, 2);
    assert.deepEqual(await page.locator(".msg.user .bubble").allTextContents(), ["a red kite", "a red kite"]);
    const seeds = (await app.engine.submitted()).filter((b) => b.prompt === "a red kite").map((b) => b.seed);
    assert.equal(seeds.length, 2);
    assert.notEqual(seeds[0], seeds[1]);

    await lastReply(page).getByRole("button", { name: "Redo with FLUX.2 klein 4B" }).click();
    await imageShown(page, 3);
    const calls = await app.engine.calls();
    assert.ok(
      calls.some(([, path]) => path === "/e/klein4b/sdcpp/v1/img_gen"),
      "the other engine was asked",
    );
    assert.match(await page.locator("#model-chip").getAttribute("aria-label"), /FLUX\.2 klein 4B/);

    await lastReply(page).getByRole("button", { name: "Reuse prompt" }).click();
    assert.equal(await imagePrompt(page).inputValue(), "a red kite");
    await context.close();
  });

  it("warns once about lettering on the model that misspells it, and offers the other one", async () => {
    const { page, context } = await open(browser, app);
    await chooseImageModel(page, "FLUX.2 klein 4B");
    await make(page, 'a neon sign that says "OPEN"');
    await imageShown(page);
    const note = lastReply(page).locator(".notice-inline");
    assert.match(await note.textContent(), /Text in images is often misspelled on this model\./);
    await note.getByRole("button", { name: "Redo with Z-Image Turbo" }).waitFor();

    await make(page, "a quiet harbour");
    await imageShown(page, 2);
    assert.equal(await lastReply(page).locator(".notice-inline").count(), 0, "no lettering asked for, no note");
    await context.close();
  });

  it("loops the bars for a model that finishes in under ten seconds", async () => {
    const { page, context } = await open(browser, app);
    await chooseImageModel(page, "FLUX.2 klein 4B");
    await make(page, "holdimage short");
    await slate(page).waitFor();
    await label(page).filter({ hasText: "About 8 s" }).waitFor();
    assert.equal(await slate(page).getAttribute("data-loop"), "");
    await app.engine.release(20);
    await imageShown(page);
    await context.close();
  });
});

describe("a job in progress", () => {
  it("holds an image where it is: nothing moves until the engine is released", async () => {
    const { page, context } = await open(browser, app);
    await chooseImageModel(page);
    await make(page, "holdimage stays put");
    await label(page).filter({ hasText: "Step 1 of 8" }).waitFor();
    // Longer than a whole unheld image takes (about 2.5 s), so a permit that leaked in would show as an image.
    await assert.rejects(images(page).first().waitFor({ timeout: 3500 }), /Timeout/);
    assert.equal(await label(page).textContent(), "Step 1 of 8");
    await app.engine.release(20);
    await imageShown(page);
    await context.close();
  });

  it("warns about a stall even when a polled chat hears the same progress again and again", async () => {
    const { page, context } = await open(browser, app);
    const text = await page.evaluate(async () => {
      const { ReplyView } = await import("/js/conversation.js");
      const view = new ReplyView({ image: { shape: "square", est: 23 } });
      document.body.append(view.el);
      view.progress({ phase: "sampling", step: 1, steps: 8 });
      const real = performance.now.bind(performance);
      performance.now = () => real() + 31_000; // thirty-one seconds pass with nothing new
      view.progress({ phase: "sampling", step: 1, steps: 8 }); // the same answer to another poll
      const shown = view.el.querySelector(".img-label").textContent;
      performance.now = real;
      view.discard();
      return shown;
    });
    assert.equal(text, "Taking longer than usual. The GPU may be busy or stuck. Stop to give up.");
    await context.close();
  });

  it("moves keyboard focus to the image when the progress bar it was on gives way to it", async () => {
    const { page, context } = await open(browser, app);
    await chooseImageModel(page);
    await make(page, "holdimage focus");
    await label(page).filter({ hasText: "Step 1 of 8" }).waitFor();
    await slate(page).focus();
    await app.engine.release(20);
    await imageShown(page);
    assert.equal(await page.evaluate(() => document.activeElement?.className), "img-out");
    await context.close();
  });

  it("does the same for a chat that was left and came back to", async () => {
    const { page, context } = await open(browser, app);
    await chooseImageModel(page);
    await make(page, "holdimage focus again");
    await label(page).filter({ hasText: "Step 1 of 8" }).waitFor();
    await page.locator("#new-chat").click();
    await page.getByRole("link", { name: /holdimage focus again/ }).click();
    await label(page)
      .filter({ hasText: /Step \d of 8/ })
      .waitFor();
    await slate(page).focus();
    await app.engine.release(20);
    await imageShown(page);
    assert.equal(await page.evaluate(() => document.activeElement?.className), "img-out");
    await context.close();
  });

  it("keeps asking while the network is gone, and shows the image once it is back and stored", async () => {
    const through = await relay(app.url);
    try {
      const { page, context } = await open(browser, { ...app, url: through.url });
      await chooseImageModel(page);
      await make(page, "holdimage dropped stream");
      await label(page).filter({ hasText: "Step 1 of 8" }).waitFor();
      through.down = true; // not just this stream: nothing gets through
      through.cut();
      await page.locator(".toast").filter({ hasText: "The connection dropped" }).waitFor();
      assert.match(page.url(), /#\/c\/[0-9a-f]{16}$/, "still in the chat");
      await page.getByRole("link", { name: /holdimage dropped stream/ }).waitFor();
      through.down = false;
      await app.engine.release(20);
      await imageShown(page);
      await context.close();
    } finally {
      through.close();
    }
  });

  it("shows each step, and Stop asks the job to stop, which ends the reply as stopped", async () => {
    const { page, context, problems } = await open(browser, app);
    await chooseImageModel(page);
    await make(page, "holdimage then stop");
    await label(page).filter({ hasText: "Step 1 of 8" }).waitFor();
    assert.equal(await slate(page).getAttribute("aria-valuetext"), "Step 1 of 8");

    await app.engine.release(1);
    await label(page).filter({ hasText: "Step 2 of 8" }).waitFor();

    const stopRequest = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/image/stop"));
    await stopButton(page).click();
    await stopRequest;
    await lastReply(page).getByText("Stopped before it finished.").waitFor();
    await announced(page, /^Image stopped$/);
    await createButton(page).waitFor();
    assert.ok(
      (await app.engine.calls()).some(([, path]) => path === "/launcher/kill"),
      "the engine was told to stop",
    );
    assert.deepEqual(await problems(), []);
    await context.close();
  });

  it("keeps going when you leave the chat, and shows its progress and its image when you come back", async () => {
    const { page, context, requests, problems } = await open(browser, app);
    await chooseImageModel(page);
    await make(page, "holdimage and walk away");
    await label(page).filter({ hasText: "Step 1 of 8" }).waitFor();

    const kills = (await app.engine.calls()).filter(([, path]) => path === "/launcher/kill").length;
    await page.locator("#new-chat").click();
    await page.getByRole("heading", { name: /^New chat on / }).waitFor();
    await page.getByRole("textbox", { name: "Message" }).waitFor(); // a new chat starts on a text model
    assert.equal(requests.filter((url) => url.endsWith("/image/stop")).length, 0, "leaving is not stopping");

    await page.getByRole("link", { name: /holdimage and walk away/ }).click();
    await slate(page).waitFor();
    await label(page)
      .filter({ hasText: /Step \d of 8/ })
      .waitFor();
    await stopButton(page).waitFor();

    await app.engine.release(20);
    await imageShown(page);
    assert.equal(await slate(page).count(), 0);
    const after = (await app.engine.calls()).filter(([, path]) => path === "/launcher/kill").length;
    assert.equal(after, kills, "nothing stopped the engine");
    // The one failed request is the stream the page dropped when it left: the job does not need it.
    assert.deepEqual(await problems(), [`requestfailed: ${app.url}/api/chat`]);
    await context.close();
  });
});

describe("when the engine fails", () => {
  it("says what went wrong and offers a retry or another model", async () => {
    const { page, context, problems } = await open(browser, app);
    await chooseImageModel(page);
    await make(page, "failimage please");
    const notice = lastReply(page).getByRole("alert");
    await notice.waitFor();
    assert.match(await notice.textContent(), /Not enough free GPU memory/);
    assert.match(await notice.textContent(), /The GPU is short of memory \(950 MB free\)\./);
    await notice.getByRole("button", { name: "Retry" }).waitFor();
    await notice.getByRole("button", { name: "Choose another model" }).waitFor();
    await announced(page, /^Image failed\. The GPU is short of memory/);
    assert.deepEqual(await problems(), []);
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

  it("image mode, empty", () => scan("empty", {}, (page) => chooseImageModel(page)));
  it("the model picker with its image group", () =>
    scan("picker", {}, (page) => page.getByRole("button", { name: /Change model$/ }).click()));
  it("the shape menu", () =>
    scan("shape menu", {}, async (page) => {
      await chooseImageModel(page);
      await shapeChip(page).click();
    }));
  it("an image being made", () =>
    scan("progress", {}, async (page) => {
      await chooseImageModel(page);
      await make(page, "holdimage a11y");
      await label(page).filter({ hasText: "Step 1 of 8" }).waitFor();
    }));
  it("a finished image, in the dark theme", () =>
    scan("done, dark", { scheme: "dark" }, async (page) => {
      await chooseImageModel(page);
      await make(page, "a lamp");
      await imageShown(page);
    }));
  it("a failed image", () =>
    scan("failed", {}, async (page) => {
      await chooseImageModel(page);
      await make(page, "failimage a11y");
      await lastReply(page).getByRole("alert").waitFor();
    }));
});

describe("phones", () => {
  const sizes = [
    { width: 320, height: 640 },
    { width: 360, height: 740 },
  ];
  for (const size of sizes) {
    it(`fits ${size.width} px: no sideways scroll, Shape and Send share the second row, touch targets 44 px`, async () => {
      const { page, context } = await open(browser, app, { ...size, mobile: true });
      await chooseImageModel(page);
      const row = async (selector) => (await page.locator(selector).boundingBox()).y;
      const modelRow = await row("#model-chip");
      assert.ok((await row("#shape-chip")) > modelRow, "the Shape chip is below the model chip");
      assert.ok(Math.abs((await row("#shape-chip")) - (await row("#send"))) < 6, "Shape and Send share a row");

      await make(page, "a tall tower");
      await imageShown(page);
      assert.equal(
        await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth),
        false,
        "no sideways scroll",
      );
      const buttons = lastReply(page).locator(".meta .btn");
      for (let i = 0; i < (await buttons.count()); i++) {
        const box = await buttons.nth(i).boundingBox();
        assert.ok(box.height >= 43.5, `action ${i} is ${box.height} tall`);
        assert.ok(box.x >= 0 && box.x + box.width <= size.width, `action ${i} is inside the screen`);
      }
      const img = await lastReply(page).locator(".img-out").boundingBox();
      assert.ok(img.x >= 0 && img.x + img.width <= size.width, "the image is inside the screen");
      await context.close();
    });
  }
});
