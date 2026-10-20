// Starts the fake providers and a real aihub web app against them, on free ports, and tears both down.

import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
    probe.on("error", reject);
  });
}

async function waitFor(url, what, child) {
  for (let i = 0; i < 100; i++) {
    if (child.exitCode !== null) throw new Error(`${what} exited early with code ${child.exitCode}`);
    try {
      if ((await fetch(url)).ok) return;
    } catch {
      // not listening yet
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error(`${what} did not come up at ${url}`);
}

/** keys: whether the OpenRouter and Anthropic keys are set. seedTurns: preload one chat with that many exchanges. */
export async function startApp({ keys = true, retentionDays = 30, seedTurns = 0 } = {}) {
  const [fakePort, appPort] = [await freePort(), await freePort()];
  const dir = mkdtempSync(join(tmpdir(), "aihub-e2e-"));
  const python = join(root, ".venv", "bin", "python");

  const fake = spawn(python, [join(root, "tests", "e2e", "fake_providers.py"), String(fakePort)], { stdio: "ignore" });
  const fakeUrl = `http://127.0.0.1:${fakePort}`;
  await waitFor(`${fakeUrl}/api/tags`, "fake providers", fake);

  const config = join(dir, "web.yaml");
  writeFileSync(
    config,
    [
      `port: ${appPort}`,
      `retention_days: ${retentionDays}`,
      `ollama: { url: "${fakeUrl}" }`,
      `openrouter: { url: "${fakeUrl}/api/v1" }`,
      `anthropic: { url: "${fakeUrl}" }`,
    ].join("\n"),
  );
  let seededChat = null;
  if (seedTurns) {
    const done = spawnSync(python, [join(root, "tests", "e2e", "seed.py"), join(dir, "state"), String(seedTurns)], {
      encoding: "utf8",
    });
    if (done.status !== 0) throw new Error(`seeding failed: ${done.stderr}`);
    seededChat = done.stdout.trim();
  }
  const env = {
    PATH: process.env.PATH,
    HOME: process.env.HOME,
    PYTHONPATH: join(root, "app"),
    AIHUB_WEB_CONFIG: config,
    AIHUB_STATE_DIR: join(dir, "state"),
    ...(keys ? { AIHUB_OPENROUTER_KEY: "sk-e2e", AIHUB_ANTHROPIC_KEY: "sk-e2e" } : {}),
  };
  const app = spawn(python, ["-m", "aihub_web"], { env, stdio: process.env.E2E_LOG ? "inherit" : "ignore" });
  const url = `http://127.0.0.1:${appPort}`;
  await waitFor(`${url}/api/health`, "aihub", app);

  return {
    url,
    fakeUrl,
    seededChat,
    requests: async () => (await fetch(`${fakeUrl}/__requests`)).json(),
    stop: () => {
      app.kill("SIGTERM");
      fake.kill("SIGTERM");
    },
  };
}
