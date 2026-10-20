import assert from "node:assert/strict";
import { after, before, describe, it } from "node:test";
import { chromium } from "playwright";
import { startApp } from "./harness.mjs";
import {
  chooseIntensity,
  chooseModel,
  composer,
  lastReply,
  open,
  replies,
  replyDone,
  send,
  sendButton,
  stopButton,
} from "./helpers.mjs";

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

const sentTo = async (path) => (await app.requests()).filter((r) => r.path === path);
const waitForReplies = (page, count) =>
  page.waitForFunction(
    (n) => document.querySelectorAll(".msg.assistant .meta").length === n && !document.querySelector(".working"),
    count,
  );

describe("a new chat", () => {
  it("starts on the free default and says where the prompt goes", async () => {
    const { page, context } = await open(browser, app);
    assert.equal(await page.locator("#model-name").textContent(), "Gemma 4 31B");
    assert.equal(await page.locator("#model-tier").textContent(), "Free");
    assert.match(await page.locator("#route-text").textContent(), /Free model via OpenRouter/);
    assert.equal(
      await page.getByRole("button", { name: /^Intensity:/ }).count(),
      0,
      "no reasoning control on this model",
    );
    await context.close();
  });

  it("goes back to the free default after a chat on Claude", async () => {
    const { page, context } = await open(browser, app);
    await chooseModel(page, "Haiku 5.5");
    assert.equal(await page.locator("#model-tier").textContent(), "Paid");
    await page.getByRole("button", { name: "New chat" }).click();
    assert.equal(await page.locator("#model-name").textContent(), "Gemma 4 31B");
    await context.close();
  });
});

describe("replying", () => {
  it("shows the message at once, streams the reply, and stores the chat", async () => {
    const { page, context } = await open(browser, app);
    await composer(page).fill("hello there");
    const shown = await page.evaluate(
      () =>
        new Promise((resolve) => {
          const t0 = performance.now();
          new MutationObserver((_, observer) => {
            if (document.querySelector(".msg.user")) {
              observer.disconnect();
              resolve(performance.now() - t0);
            }
          }).observe(document.getElementById("messages"), { childList: true });
          document.getElementById("send").click();
        }),
    );
    assert.ok(shown < 150, `the message took ${shown.toFixed(0)} ms to appear`);
    await replyDone(page);
    assert.match(
      await lastReply(page).locator(".body").textContent(),
      /Hello from the fake model\. You said: hello there/,
    );
    assert.match(page.url(), /#\/c\/[0-9a-f]{16}$/);
    assert.equal(await page.getByRole("link", { name: "hello there" }).count(), 1);

    const [call] = (await sentTo("/api/v1/chat/completions")).slice(-1);
    assert.equal(call.body.model, "google/gemma-4-31b-it:free");
    assert.equal(call.has_key, true);
    assert.equal("reasoning" in call.body, false);
    await context.close();
  });

  it("ends each reply stream cleanly, with no request the browser calls cancelled", async () => {
    // Chromium marks a finished streamed fetch as cancelled when it is sent Cache-Control: no-store. That
    // happened about nine times in ten, so three replies in one page make a regression near-certain to show.
    const { page, context, problems } = await open(browser, app);
    for (const text of ["one", "two", "three"]) {
      await send(page, text);
      await page.waitForFunction(
        (n) => document.querySelectorAll(".msg.assistant .meta").length === n && !document.querySelector(".working"),
        ["one", "two", "three"].indexOf(text) + 1,
      );
    }
    assert.deepEqual(await problems(), []);
    await context.close();
  });

  it("keeps the chat after a reload", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "remember this one");
    await replyDone(page);
    await page.reload();
    await page.getByRole("textbox", { name: "Message" }).waitFor();
    assert.equal(await page.locator("#title").textContent(), "remember this one");
    assert.match(await replies(page).first().locator(".body").textContent(), /You said: remember this one/);
    await context.close();
  });

  it("sends with Enter, adds a line with Shift+Enter", async () => {
    const { page, context } = await open(browser, app);
    await composer(page).fill("first line");
    await page.keyboard.press("Shift+Enter");
    await page.keyboard.type("second line");
    assert.equal(await composer(page).inputValue(), "first line\nsecond line");
    await page.keyboard.press("Enter");
    await replyDone(page);
    assert.equal(await composer(page).inputValue(), "");
    assert.match(await page.locator(".msg.user .bubble").textContent(), /first line\nsecond line/);
    await context.close();
  });

  it("Retry replaces the last reply instead of adding one", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "retry me");
    await replyDone(page);
    await lastReply(page).getByRole("button", { name: "Retry reply" }).click();
    await page.waitForFunction(
      () => document.querySelectorAll(".msg.assistant .meta").length === 1 && !document.querySelector(".working"),
    );
    assert.equal(await replies(page).count(), 1);
    assert.equal(await page.locator(".msg.user").count(), 1);
    await context.close();
  });

  it("Stop keeps what streamed and says so, also after a reload", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "slow please");
    await page.waitForFunction(() => document.querySelector(".msg.assistant .body")?.textContent.includes("three"));
    await stopButton(page).click();
    await lastReply(page).getByText("Stopped before it finished.").waitFor();
    await sendButton(page).waitFor();
    const partial = await lastReply(page).locator(".body").textContent();
    assert.ok(partial.startsWith("one two") && !partial.includes("forty"), partial);
    await page.reload();
    await page.getByRole("textbox", { name: "Message" }).waitFor();
    await lastReply(page).getByText("Stopped before it finished.").waitFor();
    const restored = (await lastReply(page).locator(".body").textContent()).trim();
    assert.ok(restored.startsWith(partial.trim()) && !restored.includes("forty"), restored);
    await context.close();
  });

  it("shows a provider failure mid-reply with the partial text and a way to retry", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "error please");
    const banner = lastReply(page).getByRole("alert");
    await banner.waitFor();
    assert.match(await banner.textContent(), /llama runner process has terminated/);
    assert.match(await lastReply(page).locator(".body").textContent(), /Partial reply/);
    await banner.getByRole("button", { name: "Retry" }).click();
    await lastReply(page).getByRole("alert").waitFor();
    assert.equal(await page.getByRole("alert").count(), 1);
    await context.close();
  });

  it("shows a provider's rate limit as a banner in its own words and keeps your message", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "ratelimit please");
    const banner = lastReply(page).getByRole("alert");
    await banner.waitFor();
    assert.match(await banner.textContent(), /Rate limit reached/);
    assert.match(await banner.textContent(), /Rate limit exceeded: free-models-per-day/);
    assert.equal(await page.locator(".msg.user .bubble").textContent(), "ratelimit please");
    await banner.getByRole("button", { name: "Choose another model" }).click();
    await page.getByRole("combobox", { name: "Search models" }).waitFor();
    await context.close();
  });

  it("offers Retry on the newest failed reply only, while chatting and after a reload", async () => {
    // Retry redoes the newest reply. On an older failure it used to delete the reply that came after it.
    const { page, context } = await open(browser, app);
    await send(page, "error: the old failure");
    const old = replies(page).first();
    await old.getByRole("alert").waitFor();
    assert.equal(await old.getByRole("button", { name: "Retry" }).count(), 1, "the newest reply can be retried");

    await send(page, "a question that works");
    await page.waitForFunction(() => document.querySelectorAll(".msg.assistant .meta").length === 2);
    await page.waitForFunction(() => !document.querySelector(".working"));
    assert.equal(await old.getByRole("alert").count(), 1, "the old failure is still shown");
    assert.equal(await old.getByRole("button").count(), 0, "but it no longer offers to redo anything");

    await page.reload();
    await replies(page).nth(1).waitFor();
    assert.equal(await replies(page).first().getByRole("button").count(), 0, "also when loaded from storage");
    assert.equal(await lastReply(page).getByRole("button", { name: "Retry reply" }).count(), 1);
    await context.close();
  });

  it("refuses a Retry from a window that is behind, and keeps the newest reply", async () => {
    const ahead = await open(browser, app);
    await send(ahead.page, "stale window, first question");
    await replyDone(ahead.page);
    const id = ahead.page.url().split("/c/")[1];

    const behind = await open(browser, app, { hash: `#/c/${id}` });
    await lastReply(behind.page).locator(".meta").waitFor();

    await send(ahead.page, "stale window, second question");
    await waitForReplies(ahead.page, 2);
    const providerCalls = (await sentTo("/api/v1/chat/completions")).length;

    await lastReply(behind.page).getByRole("button", { name: "Retry reply" }).click();
    await behind.page.getByText(/changed in another window/).waitFor();
    assert.equal(await replies(behind.page).count(), 1, "the reply it tried to redo comes back");
    assert.equal(await behind.page.locator(".working").count(), 0);

    const stored = await (await fetch(`${app.url}/api/chats/${id}`)).json();
    assert.deepEqual(
      stored.messages.map((m) => m.role),
      ["user", "assistant", "user", "assistant"],
      "the newest reply is still stored",
    );
    assert.equal((await sentTo("/api/v1/chat/completions")).length, providerCalls, "nothing was regenerated");
    await ahead.context.close();
    await behind.context.close();
  });

  it("refuses an oversized message before streaming and gives the text back", async () => {
    const { page, context } = await open(browser, app);
    const text = "x".repeat(100_001);
    await send(page, text);
    await page.getByText(/limited to 100,000 characters/).waitFor();
    assert.equal((await composer(page).inputValue()).length, text.length);
    assert.equal(await page.locator(".msg.user").count(), 0);
    await context.close();
  });
});

describe("model and intensity", () => {
  it("sends think:false and think:true for a local model with an on/off switch", async () => {
    const { page, context } = await open(browser, app);
    await chooseModel(page, "qwen3:8b");
    assert.equal(await page.locator("#model-tier").textContent(), "Local");
    assert.match(await page.locator("#effort-name").textContent(), /^Off$/, "new chats start at the cheapest setting");
    await send(page, "plain question");
    await replyDone(page);
    let [call] = (await sentTo("/api/chat")).slice(-1);
    assert.equal(call.body.think, false);
    assert.equal(await lastReply(page).locator(".think").count(), 0);

    await chooseIntensity(page, "On");
    await send(page, "think about this");
    await replyDone(page);
    [call] = (await sentTo("/api/chat")).slice(-1);
    assert.equal(call.body.think, true);
    const think = lastReply(page).locator("details.think");
    assert.match(await think.locator("summary").textContent(), /Thought for/);
    await think.locator("summary").click();
    assert.match(await think.locator(".think-text").textContent(), /Let me think about this carefully/);
    await context.close();
  });

  it("offers Claude's five levels and sends the chosen one with summarised thinking", async () => {
    const { page, context } = await open(browser, app);
    await chooseModel(page, "Haiku 5.5");
    await page.getByRole("button", { name: /^Intensity:/ }).click();
    const levels = await page.getByRole("radio").allTextContents();
    assert.deepEqual(
      levels.map((t) => t.trim()),
      ["Off", "Low", "Medium", "High", "Extra high", "Max"],
    );
    await page.getByRole("radio", { name: "High", exact: true }).click();
    await send(page, "hello claude");
    await replyDone(page);
    const [call] = (await sentTo("/v1/messages")).slice(-1);
    assert.equal(call.body.output_config.effort, "high");
    assert.deepEqual(call.body.thinking, { type: "adaptive", display: "summarized" });
    assert.match(await lastReply(page).locator(".meta").textContent(), /\$0\.0000\d+/, "a paid reply shows its cost");
    await context.close();
  });

  it("turns thinking off the way each Claude model accepts", async () => {
    const { page, context } = await open(browser, app);
    await chooseModel(page, "Haiku 5.5");
    await chooseIntensity(page, "Off");
    await send(page, "no thinking");
    await replyDone(page);
    let [call] = (await sentTo("/v1/messages")).slice(-1);
    assert.deepEqual(call.body.thinking, { type: "disabled" });
    assert.equal("output_config" in call.body, false);

    await page.getByRole("button", { name: "New chat" }).click();
    await chooseModel(page, "Sonnet 5.5");
    await chooseIntensity(page, "Off");
    await send(page, "no thinking either");
    await replyDone(page);
    [call] = (await sentTo("/v1/messages")).slice(-1);
    assert.deepEqual(call.body.thinking, { type: "between_tools" });

    await page.getByRole("button", { name: "New chat" }).click();
    await chooseModel(page, "Opus 5.5");
    await page.getByRole("button", { name: /^Intensity:/ }).click();
    assert.equal(
      await page.getByRole("radio", { name: "Off", exact: true }).count(),
      0,
      "Opus 5.5 cannot switch thinking off",
    );
    await context.close();
  });

  it("filters the picker by typing and picks with the keyboard", async () => {
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: /Change model$/ }).click();
    await page.getByRole("combobox", { name: "Search models" }).fill("opus");
    assert.equal(await page.getByRole("option").count(), 1);
    await page.keyboard.press("Enter");
    assert.equal(await page.locator("#model-name").textContent(), "Opus 5.5");
    assert.equal(await page.evaluate(() => document.activeElement?.id), "model-chip", "focus returns to the chip");
    await context.close();
  });

  it("lists unusable models with the reason, and never defaults to a paid one", async () => {
    const keyless = await startApp({ keys: false });
    const { page, context } = await open(browser, keyless);
    assert.equal(await page.locator("#model-tier").textContent(), "Local");
    await page.getByRole("button", { name: /Change model$/ }).click();
    const opus = page.getByRole("option", { name: /Opus 5.5/ });
    assert.equal(await opus.getAttribute("aria-disabled"), "true");
    assert.match(await opus.textContent(), /AIHUB_ANTHROPIC_KEY/);
    await context.close();
    keyless.stop();
  });
});

describe("the chat list", () => {
  it("renames, searches and deletes", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "zebra crossing rules");
    await replyDone(page);

    const row = page.locator(".chat-row", { has: page.getByRole("link", { name: "zebra crossing rules" }) });
    await row.getByRole("button", { name: /^Options for/ }).click();
    await page.getByRole("menuitem", { name: "Rename…" }).click();
    await page.getByRole("textbox", { name: "Chat title" }).fill("Crossings");
    await page.keyboard.press("Enter");
    await page.getByRole("link", { name: "Crossings" }).waitFor();
    assert.equal(await page.locator("#title").textContent(), "Crossings");

    await page.getByRole("searchbox", { name: "Search chats" }).fill("fake model");
    await page.getByText("You said: zebra", { exact: false }).first().waitFor();
    await page.getByRole("searchbox", { name: "Search chats" }).fill("no such words anywhere");
    await page.getByText(/Nothing matches/).waitFor();
    await page.getByRole("searchbox", { name: "Search chats" }).fill("");

    await page
      .locator(".chat-row", { has: page.getByRole("link", { name: "Crossings" }) })
      .getByRole("button", { name: /^Options for/ })
      .click();
    await page.getByRole("menuitem", { name: "Delete…" }).click();
    await page.getByRole("dialog", { name: "Delete this chat?" }).getByRole("button", { name: "Delete chat" }).click();
    await page.getByRole("link", { name: "Crossings" }).waitFor({ state: "detached" });
    assert.equal(await page.locator("#title").textContent(), "New chat");
    await context.close();
  });

  it("Cancel keeps the chat", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "keep me");
    await replyDone(page);
    const row = page.locator(".chat-row", { has: page.getByRole("link", { name: "keep me" }) });
    await row.getByRole("button", { name: /^Options for/ }).click();
    await page.getByRole("menuitem", { name: "Delete…" }).click();
    await page.getByRole("dialog").getByRole("button", { name: "Cancel" }).click();
    assert.equal(await page.getByRole("link", { name: "keep me" }).count(), 1);
    await context.close();
  });

  it("leaving a chat while it streams stops that reply and keeps the part that arrived", async () => {
    const { page, context } = await open(browser, app);
    await send(page, "slow and steady");
    await page.waitForFunction(() => document.querySelector(".msg.assistant .body")?.textContent.includes("two"));
    const id = page.url().split("/c/")[1];
    await page.getByRole("button", { name: "New chat" }).click();
    await page.waitForFunction(() => document.getElementById("title").textContent === "New chat");
    const stored = await (await fetch(`${app.url}/api/chats/${id}`)).json();
    const reply = stored.messages.at(-1);
    assert.equal(reply.status, "stopped");
    assert.match(reply.content, /^one two/);
    await context.close();
  });
});

describe("settings", () => {
  it("applies the theme at once and remembers it across a reload", async () => {
    const { page, context } = await open(browser, app, { scheme: "light" });
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByRole("radio", { name: "Dark" }).check();
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    await page.keyboard.press("Escape");
    await page.reload();
    assert.equal(await page.evaluate(() => document.documentElement.dataset.theme), "dark");
    assert.equal(await page.evaluate(() => getComputedStyle(document.body).colorScheme), "dark");
    await context.close();
  });

  it("saves standing instructions that reach the model", async () => {
    const { page, context } = await open(browser, app);
    await page.getByRole("button", { name: "Settings" }).click();
    await page.getByRole("textbox", { name: "Standing instructions" }).fill("Always answer in French.");
    await page.getByRole("button", { name: "Save instructions" }).click();
    await page.getByText("Instructions saved.").waitFor();
    await page.keyboard.press("Escape");
    await send(page, "bonjour");
    await replyDone(page);
    const [call] = (await sentTo("/api/v1/chat/completions")).slice(-1);
    assert.match(call.body.messages[0].content, /Always answer in French\./);
    await context.close();
  });
});
