// Run inside desktop/ with: npm test
//
// The real backend (.venv/bin/python -m aihub_web) runs against tests/e2e/fake_providers.py, so no test
// reaches a real model provider, and every Backend gets a temporary data directory and a fixture keys file
// (never ~/.config/aihub/aihub.env). Where a real Python cannot be made to misbehave on demand, a shell
// script stands in for the interpreter.
import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { after, before, describe, it } from "node:test";
import { setTimeout as sleep } from "node:timers/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import { Backend, LOG_LIMIT_BYTES, READY_TIMEOUT_MS, STOP_TIMEOUT_MS } from "../src/backend.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = join(HERE, "..", "..");
const PYTHON = join(REPO, ".venv", "bin", "python");
const FAKE_PROVIDERS = join(REPO, "tests", "e2e", "fake_providers.py");
const BACKEND_MODULE = pathToFileURL(join(HERE, "..", "src", "backend.js")).href;

// Everything below is made up. Only the first two keys may reach the backend.
const OPENROUTER_KEY = "sk-or-fixture-0001";
const ANTHROPIC_KEY = "sk-ant-fixture-0002";
const DISCORD_TOKEN = "fixture-discord-token-0003";
const GITHUB_TOKEN = "fixture-github-token-0004";
const KEYS_FILE = [
  "# fixture keys file for the desktop tests",
  `AIHUB_OPENROUTER_KEY=${OPENROUTER_KEY}`,
  `AIHUB_ANTHROPIC_KEY="${ANTHROPIC_KEY}"`,
  `export DISCORD_TOKEN=${DISCORD_TOKEN}`,
  `GITHUB_TOKEN=${GITHUB_TOKEN}`,
  "AIHUB_LAUNCH_TOKEN=token-from-the-file",
  "AIHUB_PORT=1",
  "AIHUB_WEB_CONFIG=/nonexistent/from-the-file.yaml",
  "PYTHONPATH=/nonexistent/from-the-file",
  "",
].join("\n");

const PARENT_SECRETS = [
  "parent-discord-token-0005",
  "parent-anthropic-key-0006",
  "parent-openrouter-key-0007",
  "parent-launch-token-0008",
];
const PARENT_ENV = {
  PATH: "/usr/bin:/bin",
  USER: "tester",
  LANG: "C.UTF-8",
  LC_ALL: "C.UTF-8",
  LC_CTYPE: "C.UTF-8",
  // none of these may reach the backend
  DISCORD_TOKEN: PARENT_SECRETS[0],
  ANTHROPIC_API_KEY: PARENT_SECRETS[1],
  AIHUB_OPENROUTER_KEY: PARENT_SECRETS[2],
  AIHUB_LAUNCH_TOKEN: PARENT_SECRETS[3],
  SSH_AUTH_SOCK: "/run/parent/ssh.sock",
  LD_LIBRARY_PATH: "/parent/lib",
  PYTHONSTARTUP: "/parent/startup.py",
  https_proxy: "http://127.0.0.1:9",
};

const READY_LINE = `printf '{"event":"ready","port":4321}\\n'`;
const STAMP = "\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d\\.\\d{3}Z";

let fakeUrl;
let fakeProcess;
const everyBackend = new Set();

before(async () => {
  assert.ok(existsSync(PYTHON), `${PYTHON} is missing: the tests run the real backend with the repo's virtualenv`);
  const port = await freePort();
  fakeProcess = spawn(PYTHON, [FAKE_PROVIDERS, String(port)], { stdio: "ignore" });
  fakeUrl = `http://127.0.0.1:${port}`;
  await until(async () => (await ping(`${fakeUrl}/api/tags`)) === 200, 20_000, "the fake providers", fakeProcess);
});

after(async () => {
  await Promise.all([...everyBackend].map((backend) => backend.stop()));
  fakeProcess?.kill("SIGKILL");
  for (const backend of everyBackend) assert.equal(backend.pid, null, "a test left a backend running");
});

function freePort() {
  return new Promise((resolve, reject) => {
    const probe = createServer();
    probe.once("error", reject);
    probe.listen(0, "127.0.0.1", () => {
      const { port } = probe.address();
      probe.close(() => resolve(port));
    });
  });
}

/** A listener on a free port, so that binding the port fails. */
function holdPort() {
  return new Promise((resolve, reject) => {
    const holder = createServer();
    holder.once("error", reject);
    holder.listen(0, "127.0.0.1", () => {
      resolve({ port: holder.address().port, close: () => new Promise((done) => holder.close(done)) });
    });
  });
}

async function ping(url) {
  try {
    return (await fetch(url, { signal: AbortSignal.timeout(1000) })).status;
  } catch {
    return 0;
  }
}

/** Polls until `check()` is truthy. `child`, when given, ends the wait early if it dies. */
async function until(check, ms, what, child) {
  const deadline = performance.now() + ms;
  while (!(await check())) {
    if (child && child.exitCode !== null) throw new Error(`the process died while waiting for ${what}`);
    if (performance.now() > deadline) throw new Error(`gave up after ${ms} ms waiting for ${what}`);
    await sleep(20);
  }
}

/** Whether the process runs. A zombie that only waits to be reaped counts as gone. */
function isAlive(pid) {
  try {
    process.kill(pid, 0);
  } catch (error) {
    return error.code === "EPERM";
  }
  if (process.platform !== "linux") return true;
  try {
    return !readFileSync(`/proc/${pid}/stat`, "utf8")
      .replace(/^.*\) /, "")
      .startsWith("Z");
  } catch {
    return false;
  }
}

function get(origin, path, token) {
  const headers = { connection: "close", ...(token ? { cookie: `aihub_t=${token}` } : {}) };
  return fetch(`${origin}${path}`, { headers, signal: AbortSignal.timeout(15_000) });
}

const count = (text, pattern) => (text.match(pattern) ?? []).length;
const quote = (text) => `'${text.replaceAll("'", "'\\''")}'`;
const lines = (file) => readFileSync(file, "utf8").split("\n").filter(Boolean);

function writeStub(path, body) {
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, `#!/bin/sh\n${body}\n`);
  chmodSync(path, 0o755);
}

/**
 * A data directory, a keys file and a way to make Backends on them. `useStub` swaps the interpreter of the
 * development layout for a shell script in a temporary repo root.
 */
function rig(t, { stub, keys = KEYS_FILE, parentEnv = PARENT_ENV } = {}) {
  const dir = mkdtempSync(join(tmpdir(), "aihub-desktop-test-"));
  const userData = join(dir, "userData");
  mkdirSync(userData);
  writeFileSync(
    join(userData, "web.yaml"),
    [
      `ollama: { url: "${fakeUrl}" }`,
      `openrouter: { url: "${fakeUrl}/api/v1" }`,
      `anthropic: { url: "${fakeUrl}" }`,
      "",
    ].join("\n"),
  );
  const keysFile = join(dir, "aihub.env");
  if (keys !== null) writeFileSync(keysFile, keys);
  const repoRoot = join(dir, "repo");
  let stubbed = false;
  const useStub = (body) => {
    writeStub(join(repoRoot, ".venv", "bin", "python"), body);
    stubbed = true;
  };
  if (stub !== undefined) useStub(stub);

  const made = [];
  t.after(async () => {
    await Promise.all(made.map((backend) => backend.stop()));
    rmSync(dir, { recursive: true, force: true });
  });
  const logs = (name) => readFileSync(join(userData, "logs", name), "utf8");
  return {
    dir,
    userData,
    keysFile,
    repoRoot,
    useStub,
    /** A new Backend on this data directory, with a launch token of its own. */
    make(overrides = {}) {
      const token = randomBytes(32).toString("base64url");
      const backend = new Backend({
        userData,
        token,
        keysFile,
        env: { ...parentEnv, HOME: dir, TMPDIR: dir },
        ...(stubbed ? { repoRoot } : {}),
        ...overrides,
      });
      made.push(backend);
      everyBackend.add(backend);
      return { backend, token };
    },
    log: () => logs("backend.log"),
    oldLog: () => logs("backend.log.1"),
    /** What desktop.json holds: a port, or any text at all. */
    rememberPort: (value) =>
      writeFileSync(
        join(userData, "desktop.json"),
        typeof value === "string" ? value : JSON.stringify({ port: value }),
      ),
    remembered: () => JSON.parse(readFileSync(join(userData, "desktop.json"), "utf8")).port,
  };
}

/** A file in the temporary directory that a stub appends to, one line per attempt. */
function counterFile(t) {
  const file = join(tmpdir(), `aihub-desktop-counter-${process.pid}-${randomBytes(4).toString("hex")}`);
  t.after(() => rmSync(file, { force: true }));
  return file;
}
const attempts = (counter) => (existsSync(counter) ? lines(counter) : []);

/** The first line a child process prints on stdout. */
function firstLine(child, ms = 30_000) {
  return new Promise((resolve, reject) => {
    let text = "";
    const timer = setTimeout(() => reject(new Error("no output from the parent helper")), ms);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk) => {
      text += chunk;
      if (text.includes("\n")) {
        clearTimeout(timer);
        resolve(text.split("\n")[0]);
      }
    });
    child.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`the parent helper exited with ${code}`));
    });
  });
}

/** The error a promise is rejected with. */
const rejection = (promise) =>
  promise.then(
    () => assert.fail("expected the promise to be rejected"),
    (error) => error,
  );

describe("the real backend", () => {
  it("reports the port from the ready line, and the API wants the launch cookie", { timeout: 60_000 }, async (t) => {
    const r = rig(t);
    const { backend, token } = r.make();
    const { port, origin } = await backend.start();

    assert.ok(Number.isInteger(port) && port > 0 && port < 65536, `port ${port}`);
    assert.equal(origin, `http://127.0.0.1:${port}`);
    assert.equal(backend.origin, origin);
    assert.ok(Number.isInteger(backend.pid) && isAlive(backend.pid));

    const refused = await get(origin, "/api/health");
    assert.equal(refused.status, 401);
    assert.deepEqual(await refused.json(), { error: { code: "not_authorised", message: "Not authorised." } });
    assert.equal((await get(origin, "/api/health", "not-the-token")).status, 401);
    const allowed = await get(origin, "/api/health", token);
    assert.equal(allowed.status, 200);
    assert.deepEqual(await allowed.json(), { ok: true });
    assert.equal((await get(origin, "/")).status, 200, "the page itself needs no cookie");

    const bootstrap = await (await get(origin, "/api/bootstrap", token)).json();
    assert.ok(bootstrap.models.length > 0, "the models come from the fake providers that web.yaml names");
  });

  it("remembers the port in desktop.json and uses it again on the next start", { timeout: 60_000 }, async (t) => {
    const r = rig(t);
    const first = r.make();
    const { port } = await first.backend.start();
    assert.deepEqual(JSON.parse(readFileSync(join(r.userData, "desktop.json"), "utf8")), { port });
    await first.backend.stop();

    const second = r.make();
    assert.equal((await second.backend.start()).port, port);
  });

  it(
    "stops within 7 s while a reply streams, and keeps what had streamed as a stopped reply",
    { timeout: 90_000 },
    async (t) => {
      const r = rig(t);
      const { backend, token } = r.make();
      const { origin } = await backend.start();
      const exits = [];
      backend.on("exit", (event) => exits.push(event));

      const response = await fetch(`${origin}/api/chat`, {
        method: "POST",
        headers: { cookie: `aihub_t=${token}`, "content-type": "application/json" },
        body: JSON.stringify({ text: "endless please" }),
        signal: AbortSignal.timeout(60_000),
      });
      assert.equal(response.status, 200);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let streamed = "";
      let chatId;
      while (streamed.split("word").length - 1 < 3) {
        const { value, done } = await reader.read();
        assert.ok(!done, "the stream ended before three words arrived");
        buffer += decoder.decode(value, { stream: true });
        for (let end = buffer.indexOf("\n"); end >= 0; end = buffer.indexOf("\n")) {
          const event = JSON.parse(buffer.slice(0, end));
          buffer = buffer.slice(end + 1);
          if (event.t === "chat") chatId = event.chat.id;
          if (event.t === "text") streamed += event.d;
        }
      }

      const began = performance.now();
      await backend.stop();
      const took = performance.now() - began;
      reader.cancel().catch(() => {}); // the server drops the connection once its grace period is over
      assert.ok(took < 7000, `stop() took ${Math.round(took)} ms`);
      assert.equal(exits.length, 1);
      assert.ok(
        exits[0].code === 0 || exits[0].signal === "SIGTERM",
        `the backend was killed: ${JSON.stringify(exits[0])}`,
      );
      assert.equal(backend.pid, null);

      const second = r.make();
      const { origin: again } = await second.backend.start();
      const { chats } = await (await get(again, "/api/chats", second.token)).json();
      assert.deepEqual(
        chats.map((chat) => chat.id),
        [chatId],
      );
      const { messages } = await (await get(again, `/api/chats/${chatId}`, second.token)).json();
      const reply = messages.find((message) => message.role === "assistant");
      assert.equal(reply.status, "stopped");
      assert.ok(reply.content.startsWith("word word word"), reply.content.slice(0, 40));
      assert.ok(reply.content.startsWith(streamed.trimEnd()), "everything that streamed is kept");
    },
  );

  it("falls back to a free port exactly once when the remembered port is taken", { timeout: 60_000 }, async (t) => {
    const r = rig(t);
    const held = await holdPort();
    t.after(held.close);
    r.rememberPort(held.port);

    const { backend, token } = r.make();
    const { port, origin } = await backend.start();
    assert.notEqual(port, held.port);
    assert.equal((await get(origin, "/api/health", token)).status, 200);
    assert.equal(r.remembered(), port, "the port that worked is the one remembered");
    assert.equal(count(r.log(), /address already in use/g), 1, r.log());
    assert.equal(count(r.log(), /port was taken/g), 1, r.log());
  });

  it("gives up after one retry when the backend cannot start on any port", { timeout: 60_000 }, async (t) => {
    const r = rig(t);
    mkdirSync(join(r.userData, "state"));
    execFileSync(PYTHON, [
      "-c",
      "import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('PRAGMA user_version = 99'); c.close()",
      join(r.userData, "state", "chats.db"),
    ]);
    r.rememberPort(await freePort());

    const { backend } = r.make();
    const error = await rejection(backend.start());
    assert.equal(error.exitCode, 3);
    assert.equal(count(r.log(), /newer than this app/g), 2, "one start on the remembered port, one on port 0");
    assert.equal(count(r.log(), /port was taken/g), 1);
    assert.equal(backend.pid, null);
  });

  it(
    "surfaces the aihub-web message of a bad setting, does not retry it, and starts once it is fixed",
    { timeout: 60_000 },
    async (t) => {
      const r = rig(t);
      writeFileSync(join(r.userData, "web.yaml"), "bogus: 1\n");
      r.rememberPort(await freePort());

      const { backend, token } = r.make();
      const error = await rejection(backend.start());
      assert.match(error.message, /^aihub-web: .*bogus/);
      assert.equal(error.exitCode, 1);
      assert.equal(count(r.log(), /aihub-web: /g), 1, "status 1 is not retried");
      assert.equal(count(r.log(), /port was taken/g), 0);
      assert.equal(backend.pid, null);

      writeFileSync(join(r.userData, "web.yaml"), "");
      const { origin } = await backend.start();
      assert.equal((await get(origin, "/api/health", token)).status, 200);
    },
  );

  it("makes the child exit within 2 s when its parent is SIGKILLed", { timeout: 60_000 }, async (t) => {
    const r = rig(t);
    const script = join(r.dir, "parent.mjs");
    writeFileSync(
      script,
      [
        `import { Backend } from ${JSON.stringify(BACKEND_MODULE)};`,
        "const backend = new Backend(JSON.parse(process.argv[2]));",
        "const { port } = await backend.start();",
        "console.log(JSON.stringify({ port, pid: backend.pid }));",
        "setInterval(() => {}, 60_000);",
        "",
      ].join("\n"),
    );
    const options = {
      userData: r.userData,
      token: randomBytes(32).toString("base64url"),
      keysFile: r.keysFile,
      env: { ...PARENT_ENV, HOME: r.dir },
    };
    const parent = spawn(process.execPath, [script, JSON.stringify(options)], { stdio: ["ignore", "pipe", "inherit"] });
    let childPid;
    t.after(() => {
      parent.kill("SIGKILL");
      if (childPid && isAlive(childPid)) process.kill(childPid, "SIGKILL");
    });

    childPid = JSON.parse(await firstLine(parent)).pid;
    assert.ok(isAlive(childPid), "the backend runs while its parent lives");

    const killedAt = performance.now();
    parent.kill("SIGKILL");
    await until(() => !isAlive(childPid), 2000, "the backend to exit after its parent was killed");
    assert.ok(performance.now() - killedAt < 2000);
  });

  it(
    "gives the child only the allowed variables: two keys from the file, no other name from it",
    { timeout: 60_000, skip: process.platform !== "linux" && "reads /proc/<pid>/environ, which only Linux has" },
    async (t) => {
      const r = rig(t);
      const { backend, token } = r.make();
      await backend.start();

      const entries = readFileSync(`/proc/${backend.pid}/environ`, "utf8").split("\0").filter(Boolean);
      const vars = Object.fromEntries(
        entries.map((entry) => [entry.slice(0, entry.indexOf("=")), entry.slice(entry.indexOf("=") + 1)]),
      );
      assert.deepEqual(Object.keys(vars).sort(), [
        "AIHUB_ANTHROPIC_KEY",
        "AIHUB_EXIT_ON_STDIN_EOF",
        "AIHUB_LAUNCH_TOKEN",
        "AIHUB_OPENROUTER_KEY",
        "AIHUB_PORT",
        "AIHUB_STATE_DIR",
        "AIHUB_WEB_CONFIG",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PATH",
        "PYTHONPATH",
        "TMPDIR",
        "USER",
      ]);
      assert.equal(vars.AIHUB_OPENROUTER_KEY, OPENROUTER_KEY);
      assert.equal(vars.AIHUB_ANTHROPIC_KEY, ANTHROPIC_KEY);
      assert.equal(vars.AIHUB_LAUNCH_TOKEN, token, "not the token from the file or from the parent");
      assert.equal(vars.AIHUB_PORT, "0");
      assert.equal(vars.AIHUB_EXIT_ON_STDIN_EOF, "1");
      assert.equal(vars.AIHUB_WEB_CONFIG, join(r.userData, "web.yaml"));
      assert.equal(vars.AIHUB_STATE_DIR, join(r.userData, "state"));
      assert.equal(vars.PYTHONPATH, join(REPO, "app"));
      assert.equal(vars.HOME, r.dir);
      const crossed = [DISCORD_TOKEN, GITHUB_TOKEN, ...PARENT_SECRETS, "token-from-the-file", "from-the-file.yaml"];
      assert.deepEqual(
        crossed.filter((secret) => Object.values(vars).some((value) => value.includes(secret))),
        [],
      );

      // Nothing the app writes down repeats a value of the environment.
      await backend.stop();
      const log = r.log();
      for (const secret of [token, OPENROUTER_KEY, ANTHROPIC_KEY, ...crossed]) {
        assert.ok(!log.includes(secret), `the log repeats ${secret.slice(0, 12)}...`);
      }
    },
  );

  for (const [name, makeEnv] of [
    ["a Finder-like environment", (dir) => ({ PATH: "/usr/bin:/bin", HOME: dir, USER: "tester", TMPDIR: dir })],
    ["an environment with nothing but PATH", () => ({ PATH: "/usr/bin:/bin" })],
  ]) {
    it(`still starts in ${name}`, { timeout: 60_000 }, async (t) => {
      const r = rig(t);
      const { backend, token } = r.make({ env: makeEnv(r.dir) });
      const { origin } = await backend.start();
      assert.equal((await get(origin, "/api/health", token)).status, 200);
    });
  }

  it("reports a backend that dies as an exit event, and starts again on request", { timeout: 60_000 }, async (t) => {
    const r = rig(t);
    const { backend, token } = r.make();
    await backend.start();
    const exits = [];
    backend.on("exit", (event) => exits.push(event));
    const pid = backend.pid;

    process.kill(pid, "SIGKILL");
    await until(() => exits.length === 1, 10_000, "the exit event");
    assert.deepEqual(exits[0], { code: null, signal: "SIGKILL" });
    assert.equal(backend.pid, null);
    assert.equal(backend.origin, null);
    assert.ok(!isAlive(pid));

    const { origin } = await backend.start();
    assert.equal((await get(origin, "/api/health", token)).status, 200);
    assert.equal(exits.length, 1, "starting again is not an exit");
  });
});

describe("a stand-in interpreter", () => {
  it("reads the ready line from stdout only: noise, bad ports, a repeat and a split line are no problem", async (t) => {
    const done = counterFile(t);
    const r = rig(t, {
      stub: [
        "echo 'starting up'",
        `echo '{"event":"progress","port":1}'`,
        "echo '[1,2,3]'",
        `echo '{"event":"ready","port":"4321"}'`,
        `echo '{"event":"ready","port":0}'`,
        `echo '{"event":"ready","port":70000}'`,
        `echo '{"event":"ready","port":4321.5}'`,
        `echo '{"event":"ready","port":4323}' >&2`, // stderr is not stdout
        `printf '{"event":"re'`,
        "sleep 0.2",
        `printf 'ady","port":4321,"extra":true}\\r\\n'`,
        `echo '{"event":"ready","port":4322}'`,
        "echo 'trailing words'",
        `touch ${quote(done)}`,
        "exec sleep 1000",
      ].join("\n"),
    });
    const { backend } = r.make();
    assert.deepEqual(await backend.start(), { port: 4321, origin: "http://127.0.0.1:4321" });
    await until(() => existsSync(done), 5000, "the stub to print its last line");
    await sleep(100);
    assert.equal(backend.origin, "http://127.0.0.1:4321", "a second ready line changes nothing");
  });

  it("does not take a ready line that only stderr printed", { timeout: 30_000 }, async (t) => {
    const r = rig(t, { stub: `echo '{"event":"ready","port":4323}' >&2\nexec sleep 1000` });
    const { backend } = r.make({ readyTimeout: 400 });
    const error = await rejection(backend.start());
    assert.match(error.message, /did not report ready within 0\.4 s/);
  });

  it("kills a child that never prints ready, at the ready timeout", { timeout: 30_000 }, async (t) => {
    const r = rig(t, { stub: "exec sleep 1000" });
    const { backend } = r.make({ readyTimeout: 300 });
    const began = performance.now();
    const outcome = rejection(backend.start());
    await until(() => backend.pid !== null, 2000, "the child to start");
    const pid = backend.pid;

    const error = await outcome;
    assert.match(error.message, /did not report ready within 0\.3 s/);
    assert.ok(performance.now() - began < 3000);
    await until(() => !isAlive(pid), 2000, "the child to be killed");
    assert.equal(backend.pid, null);
  });

  it("uses SIGKILL when the child ignores SIGTERM for longer than the stop timeout", { timeout: 30_000 }, async (t) => {
    const r = rig(t, { stub: ["trap '' TERM", READY_LINE, "while true; do sleep 0.1; done"].join("\n") });
    const { backend } = r.make({ stopTimeout: 400 });
    const exits = [];
    backend.on("exit", (event) => exits.push(event));
    await backend.start();
    const pid = backend.pid;

    const began = performance.now();
    await backend.stop();
    const took = performance.now() - began;
    assert.ok(took >= 350 && took < 3000, `stop() took ${Math.round(took)} ms`);
    assert.deepEqual(exits, [{ code: null, signal: "SIGKILL" }]);
    assert.ok(!isAlive(pid));
  });

  it("stops with SIGTERM when the child obeys it", async (t) => {
    const r = rig(t, { stub: `${READY_LINE}\nexec sleep 1000` });
    const { backend } = r.make();
    const exits = [];
    backend.on("exit", (event) => exits.push(event));
    await backend.start();
    await backend.stop();
    assert.deepEqual(exits, [{ code: null, signal: "SIGTERM" }]);
  });

  it("can be stopped before it starts, twice at once, and again afterwards", async (t) => {
    const r = rig(t, { stub: `${READY_LINE}\nexec sleep 1000` });
    const { backend } = r.make();
    await backend.stop();
    await backend.start();
    const pid = backend.pid;
    await Promise.all([backend.stop(), backend.stop()]);
    await backend.stop();
    assert.ok(!isAlive(pid));
    assert.equal(backend.pid, null);
    assert.equal(backend.origin, null);

    await backend.start(); // and it can run again
    assert.equal(backend.origin, "http://127.0.0.1:4321");
  });

  it("refuses a second start while it is running", async (t) => {
    const r = rig(t, { stub: `${READY_LINE}\nexec sleep 1000` });
    const { backend } = r.make();
    await backend.start();
    const pid = backend.pid;
    const error = await rejection(backend.start());
    assert.match(error.message, /already running/);
    assert.equal(backend.pid, pid, "the first backend is untouched");
  });

  it("ends a start in progress when it is stopped, and leaves no child", { timeout: 30_000 }, async (t) => {
    const r = rig(t, { stub: "exec sleep 1000" });
    const { backend } = r.make();
    const outcome = rejection(backend.start());
    await until(() => backend.pid !== null, 2000, "the child to start");
    const pid = backend.pid;

    await backend.stop();
    const error = await outcome;
    assert.equal(error.message, "The aihub backend was stopped before it was ready.");
    assert.ok(!isAlive(pid));
  });

  it("makes no second attempt once it was stopped, even if the first child ends with status 3", async (t) => {
    const counter = counterFile(t);
    // Ignores SIGTERM, so the child is still running when stop() is called and ends with 3 afterwards.
    const r = rig(t, { stub: `trap '' TERM\necho "$AIHUB_PORT" >> ${quote(counter)}\nsleep 0.4\nexit 3` });
    r.rememberPort(4999);
    const { backend } = r.make();
    const outcome = rejection(backend.start());
    await until(() => existsSync(counter), 5000, "the first attempt");
    await backend.stop();
    const error = await outcome;
    assert.equal(error.message, "The aihub backend was stopped before it was ready.");
    assert.deepEqual(attempts(counter), ["4999"], "no retry after stop()");
  });

  describe("exit status", () => {
    it("3 on a first run is reported at once, without a retry", async (t) => {
      const counter = counterFile(t);
      const r = rig(t, {
        stub: `echo "$AIHUB_PORT" >> ${quote(counter)}\necho 'ERROR: address already in use' >&2\nexit 3`,
      });
      const { backend } = r.make();
      const error = await rejection(backend.start());
      assert.equal(error.exitCode, 3);
      assert.match(error.message, /could not start \(exit code 3\)/);
      assert.deepEqual(attempts(counter), ["0"]);
    });

    it("3 with a remembered port is retried once with port 0, then reported", async (t) => {
      const counter = counterFile(t);
      const r = rig(t, { stub: `echo "$AIHUB_PORT" >> ${quote(counter)}\nexit 3` });
      r.rememberPort(4999);
      const { backend } = r.make();
      const exits = [];
      backend.on("exit", (event) => exits.push(event));
      const error = await rejection(backend.start());
      assert.equal(error.exitCode, 3);
      assert.deepEqual(attempts(counter), ["4999", "0"]);
      assert.deepEqual(exits, [], "a backend that never became ready did not exit, it failed to start");
    });

    it("3 with a remembered port, then success, remembers the port of the second attempt", async (t) => {
      const counter = counterFile(t);
      const r = rig(t, {
        stub: [
          `echo "$AIHUB_PORT" >> ${quote(counter)}`,
          '[ "$AIHUB_PORT" = 0 ] || exit 3',
          READY_LINE,
          "exec sleep 1000",
        ].join("\n"),
      });
      r.rememberPort(4999);
      const { backend } = r.make();
      await backend.stop(); // an earlier stop() must not suppress the retry of a later start()
      assert.equal((await backend.start()).port, 4321);
      assert.deepEqual(attempts(counter), ["4999", "0"]);
      assert.equal(r.remembered(), 4321);
    });

    it("1 is reported with the aihub-web line and never retried", async (t) => {
      const counter = counterFile(t);
      const r = rig(t, {
        stub: [
          `echo "$AIHUB_PORT" >> ${quote(counter)}`,
          "echo 'noise' >&2",
          "echo 'aihub-web: a bad setting' >&2",
          "exit 1",
        ].join("\n"),
      });
      r.rememberPort(4999);
      const { backend } = r.make();
      const error = await rejection(backend.start());
      assert.equal(error.message, "aihub-web: a bad setting");
      assert.equal(error.exitCode, 1);
      assert.deepEqual(attempts(counter), ["4999"]);
    });

    it("1 without an aihub-web line still says what happened", async (t) => {
      const r = rig(t, { stub: "exit 1" });
      const { backend } = r.make();
      const error = await rejection(backend.start());
      assert.equal(error.exitCode, 1);
      assert.match(error.message, /exit code 1/);
    });

    it("anything else before the ready line is a crash, named by its code or its signal", async (t) => {
      const r = rig(t, { stub: "exit 139" });
      const { backend } = r.make();
      const exited = await rejection(backend.start());
      assert.equal(exited.exitCode, 139);
      assert.match(exited.message, /stopped before it was ready \(exit code 139\)/);

      r.useStub("kill -USR1 $$");
      const signalled = await rejection(backend.start());
      assert.equal(signalled.signal, "SIGUSR1");
      assert.match(signalled.message, /stopped before it was ready \(signal SIGUSR1\)/);
    });

    it("is retried only for a remembered port that is a port", async (t) => {
      const notPorts = [
        "not json",
        "{}",
        "[]",
        "null",
        '{"port":"8787"}',
        '{"port":0}',
        '{"port":70000}',
        '{"port":1.5}',
      ];
      for (const content of notPorts) {
        const counter = counterFile(t);
        const r = rig(t, { stub: `echo "$AIHUB_PORT" >> ${quote(counter)}\nexit 3` });
        r.rememberPort(content);
        const { backend } = r.make();
        await rejection(backend.start());
        assert.deepEqual(attempts(counter), ["0"], content);
      }
    });
  });

  it("says where the interpreter should have been when it cannot be run", async (t) => {
    const r = rig(t);
    const nowhere = join(r.dir, "nowhere");
    const { backend } = r.make({ repoRoot: nowhere });
    const error = await rejection(backend.start());
    assert.match(error.message, /could not be started/);
    assert.ok(error.message.includes(join(nowhere, ".venv", "bin", "python")), error.message);
    assert.equal(backend.pid, null);
  });

  describe("command line and environment", () => {
    /** A stub that writes down what it was started with. */
    function recorder(r) {
      const files = { args: join(r.dir, "args.txt"), env: join(r.dir, "env.txt"), cwd: join(r.dir, "cwd.txt") };
      const body = [
        `printf '%s\\n' "$@" > ${quote(files.args)}`,
        `env | sort > ${quote(files.env)}`,
        `pwd -P > ${quote(files.cwd)}`,
        READY_LINE,
        "exec sleep 1000",
      ].join("\n");
      return {
        files,
        body,
        written: () => until(() => existsSync(files.cwd) && statSync(files.cwd).size > 0, 2000, "the stub"),
      };
    }
    const names = (file) => lines(file).map((line) => line.slice(0, line.indexOf("=")));
    const valueOf = (file, name) =>
      lines(file)
        .find((line) => line.startsWith(`${name}=`))
        ?.slice(name.length + 1);

    it("runs the development interpreter with -X pycache_prefix and PYTHONPATH, from the data directory", async (t) => {
      const r = rig(t);
      const { files, body, written } = recorder(r);
      r.useStub(body);
      const { backend } = r.make();
      await backend.start();
      await written();
      assert.deepEqual(lines(files.args), ["-X", `pycache_prefix=${join(r.userData, "pycache")}`, "-m", "aihub_web"]);
      assert.equal(valueOf(files.env, "PYTHONPATH"), join(r.repoRoot, "app"));
      assert.equal(lines(files.cwd)[0], realpathSync(r.userData));
    });

    it("runs the packaged interpreter with -I, and no PYTHONPATH", async (t) => {
      const r = rig(t);
      const { files, body, written } = recorder(r);
      const resourcesPath = join(r.dir, "resources");
      writeStub(join(resourcesPath, "backend", "bin", "python3.12"), body);
      const { backend } = r.make({ isPackaged: true, resourcesPath });
      await backend.start();
      await written();
      assert.deepEqual(lines(files.args), [
        "-I",
        "-X",
        `pycache_prefix=${join(r.userData, "pycache")}`,
        "-m",
        "aihub_web",
      ]);
      assert.ok(!names(files.env).includes("PYTHONPATH"));
    });

    for (const [name, keys] of [
      ["a keys file that does not exist", null],
      ["a keys file with blank keys", "AIHUB_OPENROUTER_KEY=\nAIHUB_ANTHROPIC_KEY=   \n"],
      ["a keys file with other names only", `DISCORD_TOKEN=${DISCORD_TOKEN}\n`],
      ["a keys file that is not an env file", "\u0001 not an env file \n=\n===\n"],
    ]) {
      it(`starts with ${name}, and passes no key`, async (t) => {
        const r = rig(t, { keys });
        const { files, body, written } = recorder(r);
        r.useStub(body);
        const { backend } = r.make();
        await backend.start();
        await written();
        const seen = names(files.env);
        for (const absent of ["AIHUB_OPENROUTER_KEY", "AIHUB_ANTHROPIC_KEY", "DISCORD_TOKEN", "GITHUB_TOKEN"]) {
          assert.ok(!seen.includes(absent), absent);
        }
        for (const present of ["AIHUB_LAUNCH_TOKEN", "AIHUB_PORT", "AIHUB_WEB_CONFIG", "AIHUB_STATE_DIR"]) {
          assert.ok(seen.includes(present), present);
        }
      });
    }

    it("never writes to the child's stdin and never closes it while the child lives", async (t) => {
      const r = rig(t);
      const received = join(r.dir, "stdin.txt");
      r.useStub(`${READY_LINE}\nexec cat > ${quote(received)}`);
      const { backend } = r.make();
      await backend.start();
      const pid = backend.pid;
      await sleep(400);
      assert.ok(isAlive(pid), "cat ends at the end of its input, so stdin is still open");
      await backend.stop();
      assert.equal(readFileSync(received, "utf8"), "");
    });
  });

  describe("the log", () => {
    it("holds every stderr line behind an ISO timestamp, also a last line without a newline", async (t) => {
      const r = rig(t, {
        stub: [
          "echo 'first line' >&2",
          "echo 'second line' >&2",
          READY_LINE,
          "printf 'no newline at the end' >&2",
          "exec sleep 1000",
        ].join("\n"),
      });
      const { backend } = r.make();
      await backend.start();
      await backend.stop();
      const log = r.log().trimEnd().split("\n");
      for (const line of log) assert.match(line, new RegExp(`^${STAMP} .`), line);
      for (const text of ["first line", "second line", "no newline at the end"]) {
        assert.equal(count(r.log(), new RegExp(`^${STAMP} ${text}$`, "gm")), 1, text);
      }
      assert.ok(log.findIndex((l) => l.endsWith("first line")) < log.findIndex((l) => l.endsWith("second line")));
    });

    it("is private to the user, like the backend's own state directory", async (t) => {
      const r = rig(t, { stub: `echo 'a line' >&2\n${READY_LINE}\nexec sleep 1000` });
      const { backend } = r.make();
      await backend.start();
      await backend.stop();
      assert.equal(statSync(join(r.userData, "logs")).mode & 0o777, 0o700);
      assert.equal(statSync(join(r.userData, "logs", "backend.log")).mode & 0o777, 0o600);
    });

    it("moves the old log to backend.log.1 at each start", async (t) => {
      const run = (word) => `echo 'from run ${word}' >&2\n${READY_LINE}\nexec sleep 1000`;
      const r = rig(t, { stub: run("one") });
      const { backend } = r.make();
      await backend.start();
      await backend.stop();
      assert.ok(!existsSync(join(r.userData, "logs", "backend.log.1")), "the first start has nothing to move");

      r.useStub(run("two"));
      await backend.start();
      await backend.stop();
      assert.match(r.log(), /from run two/);
      assert.doesNotMatch(r.log(), /from run one/);
      assert.match(r.oldLog(), /from run one/);

      r.useStub(run("three"));
      await backend.start();
      await backend.stop();
      assert.match(r.log(), /from run three/);
      assert.match(r.oldLog(), /from run two/);
      assert.doesNotMatch(r.oldLog(), /from run one/);
    });

    it("keeps both files within the cap, whole lines only, the newest in the current one", async (t) => {
      const cap = 2000;
      const r = rig(t, {
        stub: [
          READY_LINE,
          "i=0",
          'while [ $i -lt 200 ]; do echo "line $i xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx" >&2; i=$((i+1)); done',
          "exec sleep 1000",
        ].join("\n"),
      });
      const { backend } = r.make({ logLimit: cap });
      await backend.start();
      await until(
        () => existsSync(join(r.userData, "logs", "backend.log")) && /line 199 /.test(r.log()),
        5000,
        "the last line",
      );
      await backend.stop();

      for (const text of [r.log(), r.oldLog()]) {
        assert.ok(Buffer.byteLength(text) <= cap, `${Buffer.byteLength(text)} bytes`);
        for (const line of text.trimEnd().split("\n")) assert.match(line, new RegExp(`^${STAMP} `), "a line was cut");
      }
      assert.doesNotMatch(r.oldLog(), /line 0 /, "the oldest lines are gone");
      assert.match(r.log(), /line 199 /);
    });
  });
});

describe("the Backend class", () => {
  it("has the documented limits: ready in 20 s, stop in 7 s, a log of at most 5 MiB", () => {
    assert.equal(READY_TIMEOUT_MS, 20_000);
    assert.equal(STOP_TIMEOUT_MS, 7000);
    assert.equal(LOG_LIMIT_BYTES, 5 * 1024 * 1024);
  });

  it("needs a data directory and a launch token", () => {
    assert.throws(() => new Backend({ token: "t" }), TypeError);
    assert.throws(() => new Backend({ userData: "/x" }), TypeError);
    assert.throws(() => new Backend({ userData: "", token: "t" }), TypeError);
    assert.throws(() => new Backend({ userData: "/x", token: "" }), TypeError);
    assert.throws(() => new Backend(), TypeError);
  });

  it("imports nothing from Electron", () => {
    const source = readFileSync(join(HERE, "..", "src", "backend.js"), "utf8");
    assert.doesNotMatch(source, /from\s+["']electron/);
    assert.doesNotMatch(source, /require\(\s*["']electron/);
  });
});
