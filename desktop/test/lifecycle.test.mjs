// Run inside desktop/ with: npm test
//
// The Lifecycle is driven with stand-ins for the Backend, the window, the cookie jar and the dialog, which
// all write what they are asked to do into one list, in order. No Electron and no real process is involved.
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { describe, it } from "node:test";
import { pathToFileURL } from "node:url";
import { COOKIE, Lifecycle } from "../src/lifecycle.js";

const LOADING_FILE = "/app/src/loading.html";
const LOG_PATH = "/data/logs/backend.log";

const reply = (port, token) => ({ port, origin: `http://127.0.0.1:${port}`, token });

/** Lets every promise that can settle without a timer or an outside event settle. */
const settle = async () => {
  for (let i = 0; i < 4; i++) await new Promise((resolve) => setImmediate(resolve));
};

/**
 * A Lifecycle on stand-ins. `log` holds, in order, everything they were asked to do, as { type, ... }.
 * `backend.willStart(...)` queues what the next starts answer: a reply, or an Error to fail with.
 * `backend.die()` ends the running backend the way a crash does: its origin goes, then "exit" is emitted.
 */
function rig() {
  const log = [];
  const dialogs = [];
  const state = { lifecycle: null };

  const backend = Object.assign(new EventEmitter(), {
    origin: null,
    logPath: LOG_PATH,
    queue: [],
    willStart(...answers) {
      this.queue.push(...answers);
    },
    async start() {
      log.push({ type: "backend-start" });
      const answer = this.queue.shift();
      if (answer instanceof Error) throw answer;
      this.origin = answer.origin;
      return answer;
    },
    die(event = { code: null, signal: "SIGKILL" }) {
      this.origin = null;
      this.emit("exit", event);
    },
  });

  const window = {
    destroyed: false,
    url: "",
    onLoadURL: null,
    isDestroyed() {
      return this.destroyed;
    },
    async loadFile(path) {
      log.push({ type: "loadFile", path });
      this.url = pathToFileURL(path).href;
    },
    async loadURL(url) {
      log.push({ type: "loadURL", url, originAtCall: state.lifecycle.origin });
      this.url = url;
      await this.onLoadURL?.(url);
    },
  };

  const cookies = {
    failRemove: null,
    async set(cookie) {
      log.push({ type: "cookie-set", cookie, originAtCall: state.lifecycle.origin });
    },
    async remove(url, name) {
      log.push({ type: "cookie-remove", url, name });
      if (this.failRemove) throw this.failRemove;
    },
  };

  const lifecycle = new Lifecycle({
    backend,
    window,
    cookies,
    showDialog: (options) => {
      log.push({ type: "dialog", options });
      return new Promise((resolve) => dialogs.push({ options, answer: (response) => resolve({ response }) }));
    },
    quit: () => log.push({ type: "quit" }),
    fail: (error) => log.push({ type: "fail", error }),
    loadingFile: LOADING_FILE,
  });
  state.lifecycle = lifecycle;

  return {
    lifecycle,
    backend,
    window,
    cookies,
    log,
    dialogs,
    types: () => log.map((entry) => entry.type),
    all: (type) => log.filter((entry) => entry.type === type),
    first: (type) => log.find((entry) => entry.type === type),
    /** Starts the first backend and waits until its page is shown. */
    async up(port = 4001, token = "token-one") {
      backend.willStart(reply(port, token));
      await lifecycle.start();
      log.length = 0;
    },
  };
}

describe("the first start", () => {
  it("shows the loading page, starts the backend, sets the cookie of that start's token, then loads its origin", async () => {
    const r = rig();
    r.backend.willStart(reply(4001, "token-one"));
    await r.lifecycle.start();

    assert.deepEqual(r.types(), ["loadFile", "backend-start", "cookie-set", "loadURL"]);
    assert.equal(r.first("loadFile").path, LOADING_FILE);
    const set = r.first("cookie-set");
    assert.deepEqual(set.cookie, {
      url: "http://127.0.0.1:4001",
      name: COOKIE,
      value: "token-one",
      httpOnly: true,
      sameSite: "strict",
    });
    assert.equal(set.originAtCall, null, "the origin is set only once the cookie is in place");
    assert.equal(r.first("loadURL").url, "http://127.0.0.1:4001");
    assert.equal(r.first("loadURL").originAtCall, "http://127.0.0.1:4001", "and before the page is asked for");
    assert.equal(r.lifecycle.origin, "http://127.0.0.1:4001");
    assert.deepEqual(r.dialogs, []);
  });

  it("names the cookie aihub_t, which is what the backend looks for", () => {
    assert.equal(COOKIE, "aihub_t");
  });

  it("leaves the origin null and offers a restart when the backend cannot be started", async () => {
    const r = rig();
    r.backend.willStart(new Error("The aihub backend could not start (exit code 3)."));
    const done = r.lifecycle.start();
    await settle();

    assert.equal(r.lifecycle.origin, null);
    assert.equal(r.dialogs.length, 1);
    const { options } = r.dialogs[0];
    assert.equal(options.message, "aihub could not start its backend.");
    assert.equal(options.detail, `The aihub backend could not start (exit code 3).\n\nLog: ${LOG_PATH}`);
    assert.deepEqual(options.buttons, ["Restart", "Quit"]);
    assert.equal(options.defaultId, 0);
    assert.equal(options.cancelId, 1);
    assert.equal(r.all("cookie-set").length, 0);
    assert.equal(r.all("loadURL").length, 0);

    r.dialogs[0].answer(1);
    await done;
    assert.equal(r.all("quit").length, 1);
    assert.equal(r.all("backend-start").length, 1, "Quit does not start it again");
  });

  it("starts again on Restart and shows the page of the new backend", async () => {
    const r = rig();
    r.backend.willStart(new Error("first failure"), reply(4002, "token-two"));
    const done = r.lifecycle.start();
    await settle();
    r.dialogs[0].answer(0);
    await done;

    assert.equal(r.all("backend-start").length, 2);
    assert.equal(r.first("cookie-set").cookie.value, "token-two");
    assert.equal(r.first("loadURL").url, "http://127.0.0.1:4002");
    assert.equal(r.lifecycle.origin, "http://127.0.0.1:4002");
    assert.equal(r.all("quit").length, 0);
  });

  it("asks again, with the new reason, when the restart fails too", async () => {
    const r = rig();
    r.backend.willStart(new Error("first failure"), new Error("second failure"));
    const done = r.lifecycle.start();
    await settle();
    r.dialogs[0].answer(0);
    await settle();

    assert.equal(r.dialogs.length, 2);
    assert.equal(r.dialogs[1].options.message, "aihub could not start its backend.");
    assert.match(r.dialogs[1].options.detail, /^second failure\n\nLog: /);
    r.dialogs[1].answer(1);
    await done;
    assert.equal(r.all("quit").length, 1);
    assert.equal(r.lifecycle.origin, null);
  });

  it("does nothing more when the app is leaving by the time the backend is up", async () => {
    const r = rig();
    let release;
    r.backend.start = async () => {
      r.log.push({ type: "backend-start" });
      await new Promise((resolve) => (release = resolve));
      r.backend.origin = "http://127.0.0.1:4001";
      return reply(4001, "token-one");
    };
    const done = r.lifecycle.start();
    await settle();
    r.lifecycle.beginQuit();
    release();
    await done;

    assert.equal(r.all("cookie-set").length, 0);
    assert.equal(r.all("loadURL").length, 0);
    assert.equal(r.lifecycle.origin, null);
    assert.deepEqual(r.dialogs, []);
  });

  it("leaves the origin null and takes the cookie back when the backend ends while the cookie is being set", async () => {
    const r = rig();
    r.cookies.set = async (cookie) => {
      r.log.push({ type: "cookie-set", cookie });
      r.backend.die();
    };
    r.backend.willStart(reply(4001, "token-one"));
    const done = r.lifecycle.start();
    await settle();

    assert.equal(r.lifecycle.origin, null);
    assert.deepEqual(
      r.all("cookie-remove").map(({ url, name }) => ({ url, name })),
      [{ url: "http://127.0.0.1:4001", name: COOKIE }],
    );
    assert.equal(r.all("loadURL").length, 0, "no page is asked for");
    assert.equal(r.dialogs.length, 1, "one dialog, not one for the exit and one for the failed start");
    r.dialogs[0].answer(1);
    await done;
  });

  it("asks for one dialog only when the backend ends while its page loads", async () => {
    const r = rig();
    r.window.onLoadURL = async () => {
      r.backend.die();
      throw new Error("net::ERR_ABORTED");
    };
    r.backend.willStart(reply(4001, "token-one"));
    const done = r.lifecycle.start();
    await settle();

    assert.equal(r.lifecycle.origin, null);
    assert.equal(r.dialogs.length, 1);
    assert.deepEqual(
      r.all("cookie-remove").map(({ url }) => url),
      ["http://127.0.0.1:4001"],
    );
    r.dialogs[0].answer(1);
    await done;
  });

  it("passes on a page that fails to load while the backend still runs", async () => {
    const r = rig();
    r.window.onLoadURL = async () => {
      throw new Error("net::ERR_CONNECTION_REFUSED");
    };
    r.backend.willStart(reply(4001, "token-one"));
    const done = r.lifecycle.start();
    await settle();

    assert.equal(r.dialogs.length, 1);
    assert.match(r.dialogs[0].options.detail, /^net::ERR_CONNECTION_REFUSED\n\nLog: /);
    r.dialogs[0].answer(1);
    await done;
  });
});

describe("a backend that ends", () => {
  it("takes the origin at once, then the page and the cookie, and only then asks what to do", async () => {
    const r = rig();
    await r.up();
    assert.equal(r.lifecycle.origin, "http://127.0.0.1:4001");

    r.backend.die({ code: null, signal: "SIGKILL" });
    assert.equal(r.lifecycle.origin, null, "the origin is gone before the event handler has returned");
    await settle();

    assert.deepEqual(r.types(), ["loadFile", "cookie-remove", "dialog"]);
    assert.equal(r.first("loadFile").path, LOADING_FILE);
    assert.deepEqual(
      { url: r.first("cookie-remove").url, name: r.first("cookie-remove").name },
      { url: "http://127.0.0.1:4001", name: COOKIE },
    );
    assert.equal(r.dialogs[0].options.message, "The aihub backend stopped (signal SIGKILL).");
    assert.equal(r.dialogs[0].options.detail, `Log: ${LOG_PATH}`);
    assert.deepEqual(r.dialogs[0].options.buttons, ["Restart", "Quit"]);
  });

  it("names an exit code when there is no signal", async () => {
    const r = rig();
    await r.up();
    r.backend.die({ code: 139, signal: null });
    await settle();
    assert.equal(r.dialogs[0].options.message, "The aihub backend stopped (exit code 139).");
  });

  it("takes the origin and nothing else while the app is leaving", async () => {
    const r = rig();
    await r.up();
    r.lifecycle.beginQuit();
    r.backend.die({ code: 0, signal: null });
    await settle();

    assert.equal(r.lifecycle.origin, null);
    assert.deepEqual(r.log, []);
  });

  it("starts again on Restart, takes the new origin and the new token, and never loads the old address", async () => {
    const r = rig();
    await r.up(4001, "token-one");
    r.backend.die();
    await settle();
    r.backend.willStart(reply(4002, "token-two"));
    r.log.length = 0;
    r.dialogs[0].answer(0);
    await settle();

    assert.deepEqual(r.types(), ["loadFile", "backend-start", "cookie-set", "loadURL"]);
    assert.equal(r.first("cookie-set").cookie.value, "token-two");
    assert.equal(r.first("cookie-set").cookie.url, "http://127.0.0.1:4002");
    assert.equal(r.first("loadURL").url, "http://127.0.0.1:4002");
    assert.equal(r.lifecycle.origin, "http://127.0.0.1:4002");
  });

  it("starts again on the same port when the new backend gets it, with a cookie of its own", async () => {
    const r = rig();
    await r.up(4001, "token-one");
    r.backend.die();
    await settle();
    r.backend.willStart(reply(4001, "token-two"));
    r.log.length = 0;
    r.dialogs[0].answer(0);
    await settle();

    assert.equal(r.first("cookie-set").cookie.value, "token-two");
    assert.equal(r.lifecycle.origin, "http://127.0.0.1:4001");
  });

  it("quits on Quit", async () => {
    const r = rig();
    await r.up();
    r.backend.die();
    await settle();
    r.dialogs[0].answer(1);
    await settle();
    assert.equal(r.all("quit").length, 1);
    assert.equal(r.lifecycle.origin, null);
  });

  it("quits, and does not restart, when the app began to leave while the dialog was open", async () => {
    const r = rig();
    await r.up();
    r.backend.die();
    await settle();
    r.lifecycle.beginQuit();
    r.dialogs[0].answer(0);
    await settle();
    assert.equal(r.all("quit").length, 1);
    assert.equal(r.all("backend-start").length, 0);
  });

  it("opens one dialog at a time, however many times the backend ends", async () => {
    const r = rig();
    await r.up();
    r.backend.die();
    r.backend.die();
    await settle();
    assert.equal(r.dialogs.length, 1);
  });

  it("reports an error of the cleanup as a failure, not as an unhandled rejection", async () => {
    const r = rig();
    await r.up();
    r.cookies.failRemove = new Error("the jar is gone");
    r.backend.die();
    await settle();
    assert.equal(r.all("fail").length, 1);
    assert.equal(r.first("fail").error.message, "the jar is gone");
    assert.equal(r.dialogs.length, 0);
  });

  it("does not touch a window that is gone", async () => {
    const r = rig();
    await r.up();
    r.window.destroyed = true;
    r.backend.die();
    await settle();
    assert.equal(r.all("loadFile").length, 0);
    assert.equal(r.dialogs.length, 1, "the dialog does not need the window");
  });
});

describe("allows", () => {
  const PAGES = ["http://127.0.0.1:4001/", "http://127.0.0.1:4001/api/chat", "http://127.0.0.1:4001"];
  const ELSEWHERE = [
    "http://127.0.0.1:4002/",
    "http://127.0.0.1:8787/",
    "https://127.0.0.1:4001/",
    "http://localhost:4001/",
    "http://example.com/",
    "ws://127.0.0.1:4001/",
  ];

  it("refuses everything before the first backend is up", () => {
    const r = rig();
    for (const url of [...PAGES, ...ELSEWHERE]) assert.equal(r.lifecycle.allows(url), false, url);
  });

  it("lets the pages of the backend that is up through, and nothing else", async () => {
    const r = rig();
    await r.up(4001);
    for (const url of PAGES) assert.equal(r.lifecycle.allows(url), true, url);
    for (const url of ELSEWHERE) assert.equal(r.lifecycle.allows(url), false, url);
  });

  it("refuses everything from the moment the backend has ended", async () => {
    const r = rig();
    await r.up(4001);
    r.backend.die();
    for (const url of [...PAGES, ...ELSEWHERE]) assert.equal(r.lifecycle.allows(url), false, url);
  });

  it("follows the backend of a restart to its new port", async () => {
    const r = rig();
    await r.up(4001);
    r.backend.die();
    await settle();
    r.backend.willStart(reply(4002, "token-two"));
    r.dialogs[0].answer(0);
    await settle();
    assert.equal(r.lifecycle.allows("http://127.0.0.1:4002/api/chat"), true);
    assert.equal(r.lifecycle.allows("http://127.0.0.1:4001/api/chat"), false);
  });
});
