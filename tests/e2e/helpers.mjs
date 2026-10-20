import { createRequire } from "node:module";

const require = createRequire(import.meta.url);

/** A page with the app loaded. `problems()` lists console errors, failed requests and CSP violations. */
export async function open(
  browser,
  app,
  { width = 1280, height = 800, mobile = false, scheme = "light", hash = "", bypassCSP = false } = {},
) {
  const context = await browser.newContext({
    viewport: { width, height },
    isMobile: mobile,
    hasTouch: mobile,
    deviceScaleFactor: mobile ? 2 : 1,
    colorScheme: scheme,
    reducedMotion: "reduce",
    bypassCSP,
  });
  const page = await context.newPage();
  const problems = [];
  const requests = [];
  page.on("console", (m) => m.type() === "error" && problems.push(`console: ${m.text()}`));
  page.on("pageerror", (e) => problems.push(`pageerror: ${e.message}`));
  page.on("requestfailed", (r) => problems.push(`requestfailed: ${r.url()}`));
  page.on("request", (r) => requests.push(r.url()));
  await page.addInitScript(() => {
    window.__csp = [];
    document.addEventListener("securitypolicyviolation", (e) =>
      window.__csp.push(`${e.violatedDirective} ${e.blockedURI}`),
    );
  });
  await page.goto(`${app.url}/${hash}`);
  await page.waitForFunction(() => !document.getElementById("model-chip")?.disabled);
  return {
    page,
    context,
    requests,
    problems: async () => [...problems, ...(await page.evaluate(() => window.__csp)).map((v) => `csp: ${v}`)],
  };
}

export const composer = (page) => page.getByRole("textbox", { name: "Message" });
export const sendButton = (page) => page.getByRole("button", { name: "Send" });
export const stopButton = (page) => page.getByRole("button", { name: "Stop" });
export const replies = (page) => page.locator(".msg.assistant");
export const lastReply = (page) => replies(page).last();

/** Types a message and sends it with the button, which works on touch layouts too. */
export async function send(page, text) {
  await composer(page).fill(text);
  await sendButton(page).click();
}

/** Waits until the latest reply has finished (its footer or error banner is there). */
export async function replyDone(page) {
  await lastReply(page).locator(".meta").waitFor({ timeout: 15_000 });
}

export async function chooseModel(page, name) {
  await page.getByRole("button", { name: /Change model$/ }).click();
  await page.getByRole("option", { name: new RegExp(name) }).click();
}

export async function chooseIntensity(page, level) {
  await page.getByRole("button", { name: /^Intensity:/ }).click();
  await page.getByRole("radio", { name: level, exact: true }).click();
}

/** Runs axe-core (a local copy: the app's CSP would block a CDN script) and returns the violations. */
export async function axe(page) {
  await page.addScriptTag({ path: require.resolve("axe-core/axe.min.js") });
  const results = await page.evaluate(() =>
    window.axe.run(document, {
      runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"] },
    }),
  );
  return results.violations.map(
    (v) =>
      `${v.id} (${v.impact}): ${v.nodes
        .map((n) => n.target.join(" "))
        .slice(0, 4)
        .join(" | ")}`,
  );
}
