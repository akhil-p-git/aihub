// Runs the aihub_web backend as a child process and tells when it is ready.
//
// Nothing here imports Electron. What depends on the machine (packaging, the keys file, the parent
// environment, the timeouts) is a constructor option whose default is the real value, so tests can inject
// their own. The data directory has no Electron-free real value: main.js passes app.getPath("userData").
//
// The backend's side of the contract is in app/aihub_web/__main__.py:
// - it prints one line on stdout, {"event":"ready","port":N}, once it accepts connections;
// - with AIHUB_EXIT_ON_STDIN_EOF=1 it shuts down gracefully when stdin ends, which happens when this
//   process dies, however it dies, because stdin is a pipe that only this process holds;
// - exit status 3 means it never started (port taken, lifespan failed), 1 a bad setting with an
//   "aihub-web: ..." message on stderr.

import { spawn } from "node:child_process";
import { randomBytes } from "node:crypto";
import { EventEmitter } from "node:events";
import { appendFileSync, mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { parseEnv } from "node:util";
import { buildChildEnv } from "./policy.js";

export const READY_TIMEOUT_MS = 20_000;
// uvicorn waits up to 5 s for open connections before it exits; the slowest stop measured was 5.2 s.
export const STOP_TIMEOUT_MS = 7000;
export const LOG_LIMIT_BYTES = 5 * 1024 * 1024;

// A line of the backend's stderr is cut here, so that no single line can break the log's size limit.
const MAX_LOG_LINE = 16 * 1024;

// desktop/src/backend.js -> the repository root, which holds .venv and app/ in development.
const REPO_ROOT = fileURLToPath(new URL("../../", import.meta.url));

/** "exit code 3" or "signal SIGKILL". */
const describeExit = (code, signal) => (signal ? `signal ${signal}` : `exit code ${code}`);

const isPort = (value) => Number.isInteger(value) && value >= 1 && value <= 65535;

/** A launch token: 32 random bytes as base64url, which is 43 characters. */
const newToken = () => randomBytes(32).toString("base64url");

/** The port of a ready line, or null for any other line. */
function readyPort(line) {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    return null;
  }
  return message?.event === "ready" && isPort(message.port) ? message.port : null;
}

export class Backend extends EventEmitter {
  #userData;
  #makeToken;
  #isPackaged;
  #resourcesPath;
  #repoRoot;
  #keysFile;
  #env;
  #readyTimeout;
  #stopTimeout;
  #logLimit;

  #child = null;
  #origin = null;
  #starting = false;
  #stopRequested = false;
  #stopping = null;
  #logSize = 0;

  /**
   * - userData: the app's data directory. It holds web.yaml, state/, pycache/, logs/ and desktop.json.
   * - makeToken: makes the launch token, which the backend demands as the aihub_t cookie on every /api/ request.
   *   It is called once for every spawn, the retry on a free port and every restart included, so a token never
   *   outlives its backend. By default it makes 32 random bytes as base64url; a test can inject its own.
   * - isPackaged, resourcesPath: where the interpreter is. Packaged, it is <resourcesPath>/backend/bin/python3.12.
   * - repoRoot: in development, the interpreter is <repoRoot>/.venv/bin/python and the code is <repoRoot>/app.
   * - keysFile: the file the two provider keys are read from, and nothing else in it.
   * - env: the environment of this app, from which a few names are passed on.
   * - readyTimeout, stopTimeout, logLimit: milliseconds and bytes.
   *
   * Emits "exit" with { code, signal } when a backend that was ready has ended, however it ended. A backend
   * that ends before it is ready is reported by the rejection of start() instead.
   */
  constructor({
    userData,
    makeToken = newToken,
    isPackaged = false,
    resourcesPath = process.resourcesPath,
    repoRoot = REPO_ROOT,
    keysFile = join(homedir(), ".config", "aihub", "aihub.env"),
    env = process.env,
    readyTimeout = READY_TIMEOUT_MS,
    stopTimeout = STOP_TIMEOUT_MS,
    logLimit = LOG_LIMIT_BYTES,
  } = {}) {
    super();
    if (typeof userData !== "string" || userData === "") throw new TypeError("Backend needs the data directory");
    if (isPackaged && typeof resourcesPath !== "string") throw new TypeError("A packaged Backend needs resourcesPath");
    this.#userData = userData;
    this.#makeToken = makeToken;
    this.#isPackaged = isPackaged;
    this.#resourcesPath = resourcesPath;
    this.#repoRoot = repoRoot;
    this.#keysFile = keysFile;
    this.#env = env;
    this.#readyTimeout = readyTimeout;
    this.#stopTimeout = stopTimeout;
    this.#logLimit = logLimit;
  }

  /** The process id of the running backend, or null. */
  get pid() {
    return this.#child?.pid ?? null;
  }

  /** The origin the running backend answers on, like "http://127.0.0.1:41234", or null. */
  get origin() {
    return this.#origin;
  }

  get logPath() {
    return join(this.#userData, "logs", "backend.log");
  }

  /**
   * Whether the backend process is running. This turns false the moment the process has died, which can be some
   * milliseconds before "exit" is emitted, because Node learns of a death only when its event loop gets to it. A
   * process that has died and not been collected yet is a zombie, and /proc shows that at once.
   */
  isAlive() {
    const child = this.#child;
    if (child === null || child.exitCode !== null || child.signalCode !== null) return false;
    try {
      const stat = readFileSync(`/proc/${child.pid}/stat`, "latin1");
      // The state letter follows the process name, which sits in parentheses and may hold some itself.
      return !/^[ZX]/.test(stat.slice(stat.lastIndexOf(")") + 2));
    } catch {
      return false;
    }
  }

  /**
   * Starts the backend and resolves with its port, its origin and its launch token once it has printed its
   * ready line. The token is the one this spawn was given: the caller has to put it in the page's cookie,
   * and it is worth nothing to the next backend, which makes its own.
   *
   * It asks for the port of the last run, so that the origin, and with it the page's saved settings, stay
   * the same. If that port is taken (exit status 3) it tries once more with a free one, and a new token.
   */
  async start() {
    if (this.#starting || this.#child) throw new Error("The aihub backend is already running.");
    this.#starting = true;
    this.#stopRequested = false;
    try {
      this.#rotateLog();
      const keys = this.#readKeys();
      let port = this.#rememberedPort();
      for (;;) {
        try {
          const ready = await this.#launch(port ?? 0, keys);
          this.#rememberPort(ready.port);
          return ready;
        } catch (error) {
          if (error.exitCode !== 3 || port === null) throw error;
          this.#note("the backend's port was taken, starting again on a free port");
          port = null;
        }
      }
    } finally {
      this.#starting = false;
    }
  }

  /**
   * Asks the backend to stop with SIGTERM, which stores a reply that is streaming as stopped, and kills it
   * after the stop timeout. Resolves once it is gone. Safe to call at any time, any number of times.
   */
  stop() {
    this.#stopRequested = true;
    const child = this.#child;
    if (child === null) return Promise.resolve();
    this.#stopping ??= new Promise((resolve) => {
      const killer = setTimeout(() => child.kill("SIGKILL"), this.#stopTimeout);
      child.once("close", () => {
        clearTimeout(killer);
        this.#stopping = null;
        resolve();
      });
      child.kill("SIGTERM");
    });
    return this.#stopping;
  }

  /** The interpreter, its arguments, and PYTHONPATH where -I does not make the interpreter ignore it. */
  #command() {
    const pycache = `pycache_prefix=${join(this.#userData, "pycache")}`;
    if (this.#isPackaged) {
      const python = join(this.#resourcesPath, "backend", "bin", "python3.12");
      return { python, args: ["-I", "-X", pycache, "-m", "aihub_web"], pythonPath: undefined };
    }
    const python = join(this.#repoRoot, ".venv", "bin", "python");
    return { python, args: ["-X", pycache, "-m", "aihub_web"], pythonPath: join(this.#repoRoot, "app") };
  }

  /**
   * Runs the backend once, with a launch token made for this spawn alone. Resolves at the ready line; rejects
   * if it ends or times out before.
   */
  #launch(port, keys) {
    return new Promise((resolve, reject) => {
      const { python, args, pythonPath } = this.#command();
      const token = this.#makeToken();
      const env = buildChildEnv({
        parent: this.#env,
        userData: this.#userData,
        port,
        token,
        keys,
        pythonPath,
      });
      // The data directory is the working directory: without -I (development) Python puts it on its path.
      let child;
      try {
        child = spawn(python, args, { env, cwd: this.#userData, stdio: ["pipe", "pipe", "pipe"] });
      } catch (error) {
        // What spawn refuses it can quote: a key with a NUL byte in it comes back in the message, value and all.
        reject(new Error(`The aihub backend could not be started (${error.code ?? error.name}).`));
        return;
      }
      this.#child = child;
      let ready = false;
      let timedOut = false;
      const stderrTail = [];
      const readyTimer = setTimeout(() => {
        timedOut = true;
        child.kill("SIGKILL");
      }, this.#readyTimeout);

      child.on("error", (error) => {
        if (child.pid !== undefined) {
          this.#note(`talking to the backend failed (${error.code ?? error.name})`);
          return;
        }
        clearTimeout(readyTimer);
        if (this.#child === child) this.#child = null;
        reject(new Error(`The aihub backend could not be started: ${error.message}`));
      });

      createInterface({ input: child.stdout }).on("line", (line) => {
        const found = ready ? null : readyPort(line);
        if (found === null) return;
        ready = true;
        stderrTail.length = 0;
        clearTimeout(readyTimer);
        this.#origin = `http://127.0.0.1:${found}`;
        this.#note("the backend is ready");
        resolve({ port: found, origin: this.#origin, token });
      });

      createInterface({ input: child.stderr }).on("line", (line) => {
        this.#write(line);
        if (!ready) {
          stderrTail.push(line);
          if (stderrTail.length > 50) stderrTail.shift();
        }
      });

      if (child.pid !== undefined) this.#note(`starting the backend (process ${child.pid})`);
      child.once("close", (code, signal) => {
        clearTimeout(readyTimer);
        if (child.pid === undefined) return; // it never ran; the error event has rejected already
        if (this.#child === child) {
          this.#child = null;
          this.#origin = null;
        }
        this.#note(`the backend ended (${describeExit(code, signal)})`);
        if (ready) {
          this.emit("exit", { code, signal });
        } else {
          reject(this.#failure({ code, signal, timedOut, stderrTail }));
        }
      });
    });
  }

  /** Why a backend that never became ready is an error, in words for a dialog. */
  #failure({ code, signal, timedOut, stderrTail }) {
    // No exitCode on this one: that is what keeps start() from trying again on a backend that was stopped.
    if (this.#stopRequested) return new Error("The aihub backend was stopped before it was ready.");
    if (timedOut) return new Error(`The aihub backend did not report ready within ${this.#readyTimeout / 1000} s.`);
    const how = describeExit(code, signal);
    const setting = code === 1 ? stderrTail.findLast((line) => line.startsWith("aihub-web: ")) : undefined;
    const message =
      setting ??
      (code === 3
        ? `The aihub backend could not start (${how}).`
        : `The aihub backend stopped before it was ready (${how}).`);
    return Object.assign(new Error(message), { exitCode: code, signal });
  }

  /** The parsed keys file. It holds names that must not reach the backend too; buildChildEnv takes only the two keys. */
  #readKeys() {
    let text;
    try {
      text = readFileSync(this.#keysFile, "utf8");
    } catch (error) {
      if (error.code !== "ENOENT") this.#note(`could not read the keys file (${error.code})`);
      return {};
    }
    return parseEnv(text);
  }

  get #statePath() {
    return join(this.#userData, "desktop.json");
  }

  /** The port of the last run, or null when there is none worth trying. */
  #rememberedPort() {
    try {
      const { port } = JSON.parse(readFileSync(this.#statePath, "utf8"));
      return isPort(port) ? port : null;
    } catch {
      return null;
    }
  }

  #rememberPort(port) {
    try {
      writeFileSync(this.#statePath, JSON.stringify({ port }));
    } catch (error) {
      this.#note(`could not save the port (${error.code})`);
    }
  }

  /** Moves the log of the last run to backend.log.1 and starts an empty one. */
  #rotateLog() {
    // Private like the backend's own state directory: the access log can hold what the user searched for.
    mkdirSync(join(this.#userData, "logs"), { recursive: true, mode: 0o700 });
    try {
      renameSync(this.logPath, `${this.logPath}.1`);
    } catch {
      // the first start has no log to move
    }
    try {
      this.#logSize = statSync(this.logPath).size;
    } catch {
      this.#logSize = 0;
    }
  }

  /** A line of the app's own in the log. It never repeats a value of the environment. */
  #note(text) {
    this.#write(`desktop: ${text}`);
  }

  /** Appends a line behind an ISO timestamp. When the line would pass the limit, the log becomes backend.log.1. */
  #write(line) {
    const text = line.length > MAX_LOG_LINE ? `${line.slice(0, MAX_LOG_LINE)} [cut]` : line;
    const entry = `${new Date().toISOString()} ${text}\n`;
    const size = Buffer.byteLength(entry);
    try {
      if (this.#logSize > 0 && this.#logSize + size > this.#logLimit) {
        renameSync(this.logPath, `${this.logPath}.1`);
        this.#logSize = 0;
      }
      appendFileSync(this.logPath, entry, { mode: 0o600 });
      this.#logSize += size;
    } catch {
      // a log that cannot be written must not take the app down
    }
  }
}
