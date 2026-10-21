// Builds the Python that the packaged desktop app starts its backend with: desktop/payload/<target>/.
//
//   node scripts/build-backend.mjs <linux-x64|mac-arm64|mac-x64>
//
// The result is laid out as the app finds it under resources/backend/:
//
//   bin/python3.12                                 CPython from python-build-standalone, pinned by sha256
//   lib/python3.12/site-packages/aihub_web/        the app, with its static/ folder
//   lib/python3.12/site-packages/<dependency>/     every runtime dependency of uv.lock, from hashed wheels
//
// The steps: uv exports the locked runtime dependencies with their hashes; the pinned archive is downloaded
// (https only, with a timeout) and checked against its sha256 before anything is extracted; uv installs the
// wheels for the target platform; app/aihub_web is copied in; what the app does not need is pruned. All of it
// happens in a work folder next to the result, which replaces desktop/payload/<target> only after every step
// has passed, so a failed or interrupted run never leaves something that looks like a payload.
//
// No file from the archive or from a wheel is ever modified: the macOS payloads are code-signed later, and
// stripping, lipo or install_name_tool would break the seals. Bytecode is not compiled either; the app runs
// Python with -X pycache_prefix so that nothing is written into the payload.
//
// It needs only Node, uv and tar, and uses nothing specific to GNU or Linux, so it is meant to run unchanged on a
// Mac. To move to another release, change PBS_TAG, PYTHON_VERSION and the three sha256 values (they are in the
// release's SHA256SUMS file).
//
// Exit status: 0 on success, 2 for a bad command line, 1 for any other failure (one line on stderr says why).

import { spawn } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import {
  cpSync,
  createReadStream,
  createWriteStream,
  existsSync,
  lstatSync,
  mkdirSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { pipeline } from "node:stream/promises";
import { fileURLToPath } from "node:url";

export const PBS_TAG = "20261003";
export const PYTHON_VERSION = "3.12.15";
export const MAX_PBS_AGE_DAYS = 45;

const PYTHON_MINOR = "3.12";
const RELEASE_URL = `https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}`;
const DOWNLOAD_TIMEOUT_MS = 10 * 60 * 1000;
const MAX_REDIRECTS = 5;

// triple: the archive's platform and the value of uv's --python-platform.
// host: the Node platform and architecture that can run the target's interpreter.
// extension: the suffix of compiled modules (name.cpython-312-<extension>.so).
// libpython: the shared libpython in the archive (see PRUNE_PATTERNS for why it stays).
export const TARGETS = Object.freeze({
  "linux-x64": Object.freeze({
    triple: "x86_64-unknown-linux-gnu",
    sha256: "731af898886c5f821890dc901eca3c651cca8e51fa7308c159d12a1194aeac91",
    host: Object.freeze({ platform: "linux", arch: "x64" }),
    extension: "x86_64-linux-gnu",
    libpython: "lib/libpython3.12.so.1.0",
  }),
  "mac-arm64": Object.freeze({
    triple: "aarch64-apple-darwin",
    sha256: "ad8d0c637c0a36b967b310e2c07254f4d2ca8cabaa7699e55ed6290aceb481a2",
    host: Object.freeze({ platform: "darwin", arch: "arm64" }),
    extension: "darwin",
    libpython: "lib/libpython3.12.dylib",
  }),
  "mac-x64": Object.freeze({
    triple: "x86_64-apple-darwin",
    sha256: "562c30864ece2cb1d3e0ad66a1acd498611a47e5a10ce81b99158bef1ccbd355",
    host: Object.freeze({ platform: "darwin", arch: "x64" }),
    extension: "darwin",
    libpython: "lib/libpython3.12.dylib",
  }),
});

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(desktopDir, "..");
const downloadsDir = join(desktopDir, ".cache", "downloads");
const payloadDir = join(desktopDir, "payload");

/** A failure with a reason worth one line; `status` is the process exit status (2 for a bad command line). */
export class BuildError extends Error {
  constructor(message, status = 1) {
    super(message);
    this.name = "BuildError";
    this.status = status;
  }
}

// ---------------------------------------------------------------------------------------------------------------
// The command line and the pins

/** The target named by the command line, or a BuildError with status 2. */
export function parseTarget(args) {
  const names = Object.keys(TARGETS);
  if (args.length !== 1) throw new BuildError(`usage: node scripts/build-backend.mjs <${names.join("|")}>`, 2);
  if (!Object.hasOwn(TARGETS, args[0])) {
    const list = `${names.slice(0, -1).join(", ")} or ${names.at(-1)}`;
    throw new BuildError(`unknown target "${args[0]}" (expected ${list})`, 2);
  }
  return args[0];
}

export function archiveName(target) {
  return `cpython-${PYTHON_VERSION}+${PBS_TAG}-${TARGETS[target].triple}-install_only_stripped.tar.gz`;
}

/** The asset URL; the + of the file name is written %2B, which is how GitHub lists it. */
export function archiveUrl(target) {
  return `${RELEASE_URL}/${encodeURIComponent(archiveName(target))}`;
}

/** Throws unless `url` is an https URL. Never prints the query, which may carry a signed token. */
export function assertHttps(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new BuildError(`not a URL: ${url}`);
  }
  if (parsed.protocol !== "https:") {
    throw new BuildError(`refusing a ${parsed.protocol.slice(0, -1)} URL, only https is allowed: ${show(parsed)}`);
  }
  return parsed;
}

const show = (url) => `${url.host}${url.pathname}`;

/** Whole days between the release tag (YYYYMMDD) and `now`. */
export function pbsAgeDays(tag = PBS_TAG, now = new Date()) {
  const match = /^(\d{4})(\d{2})(\d{2})$/.exec(tag);
  const [year, month, day] = match ? match.slice(1).map(Number) : [];
  const released = match && new Date(Date.UTC(year, month - 1, day));
  if (!released || released.getUTCMonth() !== month - 1 || released.getUTCDate() !== day) {
    throw new BuildError(`the release tag ${tag} is not a YYYYMMDD date`);
  }
  return Math.floor((now - released) / 86_400_000);
}

export function ageWarning(days) {
  if (days <= MAX_PBS_AGE_DAYS) return null;
  const release = `python-build-standalone ${PBS_TAG} is ${days} days old (warning above ${MAX_PBS_AGE_DAYS})`;
  return `${release}; look for a newer release and re-pin it`;
}

// ---------------------------------------------------------------------------------------------------------------
// Downloading and verifying

export async function sha256File(path) {
  const hash = createHash("sha256");
  for await (const chunk of createReadStream(path)) hash.update(chunk);
  return hash.digest("hex");
}

/** Resolves when the file's sha256 is `expected`; otherwise deletes the file and throws. */
export async function verifyArchive(path, expected) {
  const actual = await sha256File(path);
  if (actual !== expected) {
    rmSync(path, { force: true });
    throw new BuildError(
      `sha256 mismatch for ${basename(path)}: expected ${expected}, got ${actual}; the file was deleted`,
    );
  }
}

/** GET with every redirect checked to be https (fetch itself would follow one to http). */
async function fetchHttps(url, signal) {
  let current = url;
  for (let hop = 0; hop <= MAX_REDIRECTS; hop++) {
    assertHttps(current);
    const response = await fetch(current, { redirect: "manual", signal });
    const location = response.headers.get("location");
    if (response.status < 300 || response.status >= 400 || !location) return response;
    await response.body?.cancel();
    current = new URL(location, current).href;
  }
  throw new BuildError(`more than ${MAX_REDIRECTS} redirects from ${show(new URL(url))}`);
}

/** Streams `url` into the file `dest`. Throws a BuildError after `timeoutMs` for the whole download. */
export async function downloadFile(url, dest, timeoutMs = DOWNLOAD_TIMEOUT_MS) {
  const signal = AbortSignal.timeout(timeoutMs);
  try {
    const response = await fetchHttps(url, signal);
    if (!response.ok || !response.body) {
      throw new BuildError(`download failed with HTTP ${response.status}: ${show(new URL(url))}`);
    }
    await pipeline(response.body, createWriteStream(dest));
  } catch (err) {
    rmSync(dest, { force: true });
    if (err instanceof BuildError) throw err;
    const where = show(new URL(url));
    if (signal.aborted) throw new BuildError(`download timed out after ${Math.round(timeoutMs / 1000)} s: ${where}`);
    throw new BuildError(`download failed (${err.cause?.code ?? err.message}): ${where}`);
  }
}

/** The verified archive in the download cache; fetched only when the cache has no file with the right sha256. */
async function fetchArchive(target) {
  const { sha256 } = TARGETS[target];
  const name = archiveName(target);
  const file = join(downloadsDir, name);
  mkdirSync(downloadsDir, { recursive: true });
  for (const entry of readdirSync(downloadsDir)) {
    if (entry.startsWith(`${name}.part-`) && isAbandoned(entry)) rmSync(join(downloadsDir, entry), { force: true });
  }

  if (existsSync(file)) {
    try {
      await verifyArchive(file, sha256);
      log(target, `${name} is in the cache and matches its sha256`);
      return file;
    } catch (err) {
      if (!(err instanceof BuildError)) throw err;
      log(target, `the cached ${name} did not match its sha256 and was deleted; downloading it again`);
    }
  }

  const part = `${file}.part-${process.pid}`;
  const forget = onInterrupt(() => rmSync(part, { force: true }));
  try {
    const started = Date.now();
    await downloadFile(archiveUrl(target), part);
    await verifyArchive(part, sha256);
    renameSync(part, file);
    log(target, `downloaded ${name} (${mib(statSync(file).size)}) in ${seconds(started)} and verified its sha256`);
  } finally {
    forget();
    rmSync(part, { force: true });
  }
  return file;
}

// ---------------------------------------------------------------------------------------------------------------
// What the app does not need

// Paths are relative to the payload, with "/" separators. A matching entry is removed with everything below it.
//
// libpython3.12.so.1.0 and libpython3.12.dylib are deliberately not listed. bin/python3.12 does not link them
// (readelf -d and llvm-otool -L list only system libraries, because the interpreter contains libpython and
// exports the C API to extension modules), so each is an unused copy: about 31 MiB on Linux, 17 MiB on macOS.
// They stay while the macOS payloads have not run on a Mac; dropping them later is one pattern here. The pinned
// archives have no static libpython (.a) and nothing of libpython under config-3.12-*, which goes as a whole.
const PRUNE_PATTERNS = [
  // Every launcher but the interpreter: pip, idle, pydoc, 2to3, the python and python3 symlinks, *-config.
  /^bin\/(?!python3\.12$)/,
  // C headers, man pages and terminfo, pkg-config files.
  /^(include|share|lib\/pkgconfig)(\/|$)/,
  // Build configuration and stdlib packages the app never imports.
  /^lib\/python3\.12\/(config-3\.12-[^/]+|ensurepip|idlelib|lib2to3|pydoc_data|tkinter|turtledemo)(\/|$)/,
  // The extension modules for crypt, dbm and Tcl/Tk, which the app never imports.
  /^lib\/python3\.12\/lib-dynload\/(_crypt|_dbm|_tkinter)[^/]*$/,
  // pip, and what `uv pip install --target` adds: console scripts with the build host's interpreter in their
  // shebang, and a world-writable lock file.
  /^lib\/python3\.12\/site-packages\/(pip|pip-[^/]+\.dist-info|bin|\.lock)(\/|$)/,
  // Tcl/Tk, which only _tkinter uses: script libraries, Tcl packages and the two shared libraries.
  /^lib\/(tcl|tk|itcl|tdbc|thread)\d[\d.]*(\/|$)/,
  /^lib\/lib(tcl|tk)[^/]*$/,
];

/** Whether the payload entry at `rel` goes: bytecode anywhere, or a match for PRUNE_PATTERNS. */
export function shouldPrune(rel) {
  return rel.endsWith(".pyc") || rel.split("/").includes("__pycache__") || PRUNE_PATTERNS.some((p) => p.test(rel));
}

/** Every entry below `root`, sorted, parents before children, without following symlinks. */
function* walk(root, rel = "") {
  const entries = readdirSync(join(root, rel), { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1));
  for (const dirent of entries) {
    const path = rel ? `${rel}/${dirent.name}` : dirent.name;
    yield { rel: path, dirent };
    if (dirent.isDirectory()) yield* walk(root, path);
  }
}

function bytesIn(path) {
  const stat = lstatSync(path);
  if (!stat.isDirectory()) return stat.size;
  return readdirSync(path).reduce((sum, name) => sum + bytesIn(join(path, name)), 0);
}

/** Deletes what shouldPrune matches below `root`. Returns how many entries went and how many bytes they held. */
export function prune(root) {
  let entries = 0;
  let bytes = 0;
  const visit = (dir, rel) => {
    for (const dirent of readdirSync(dir, { withFileTypes: true })) {
      const path = rel ? `${rel}/${dirent.name}` : dirent.name;
      const full = join(dir, dirent.name);
      if (shouldPrune(path)) {
        bytes += bytesIn(full);
        entries += 1;
        rmSync(full, { recursive: true, force: true });
      } else if (dirent.isDirectory()) {
        visit(full, path);
      }
    }
  };
  visit(root, "");
  return { entries, bytes };
}

/** Throws unless the finished payload has the files in `required` and nothing it should not have. */
export function auditPayload(payload, required = []) {
  const root = resolve(payload);
  const problems = required.filter((rel) => !existsSync(join(root, rel))).map((rel) => `missing ${rel}`);
  for (const { rel, dirent } of walk(root)) {
    const full = join(root, rel);
    if (shouldPrune(rel)) problems.push(`left over: ${rel}`);
    if (dirent.isSymbolicLink()) {
      const link = readlinkSync(full);
      if (isAbsolute(link)) problems.push(`absolute symlink: ${rel} -> ${link}`);
      else if (!resolve(dirname(full), link).startsWith(root + sep)) {
        problems.push(`symlink leaves the payload: ${rel} -> ${link}`);
      } else if (!existsSync(full)) problems.push(`dangling symlink: ${rel} -> ${link}`);
    } else if (lstatSync(full).mode & 0o002) {
      problems.push(`world-writable: ${rel}`);
    }
  }
  if (problems.length) {
    const more = problems.length > 5 ? ` (and ${problems.length - 5} more)` : "";
    throw new BuildError(`payload check failed: ${problems.slice(0, 5).join("; ")}${more}`);
  }
}

function requiredFiles(target) {
  const { extension, libpython } = TARGETS[target];
  const site = `lib/python${PYTHON_MINOR}/site-packages`;
  const tag = `cpython-${PYTHON_MINOR.replace(".", "")}-${extension}`;
  return [
    `bin/python${PYTHON_MINOR}`,
    libpython,
    `lib/python${PYTHON_MINOR}/os.py`,
    `${site}/aihub_web/__main__.py`,
    `${site}/aihub_web/static/index.html`,
    `${site}/pydantic_core/_pydantic_core.${tag}.so`,
    `${site}/yaml/_yaml.${tag}.so`,
  ];
}

// ---------------------------------------------------------------------------------------------------------------
// Building in a work folder

const cleanups = new Set();
const children = new Set();

/** Registers what to undo if the run is interrupted; returns the function that forgets it. */
function onInterrupt(cleanup) {
  cleanups.add(cleanup);
  return () => cleanups.delete(cleanup);
}

function exitOnInterrupt() {
  for (const [signal, status] of [
    ["SIGINT", 130],
    ["SIGTERM", 143],
  ]) {
    process.once(signal, () => {
      for (const child of children) child.kill("SIGTERM");
      for (const cleanup of cleanups) cleanup();
      process.exit(status);
    });
  }
}

/** Whether the process that made a leftover is gone. Its pid ends the name: -<pid> or -<pid>-<8 hex digits>. */
function isAbandoned(name) {
  const pid = /-(\d+)(?:-[0-9a-f]{8})?$/.exec(name)?.[1];
  if (!pid) return true;
  try {
    process.kill(Number(pid), 0);
    return false;
  } catch (err) {
    return err.code !== "EPERM"; // EPERM: the process exists but belongs to someone else
  }
}

/**
 * Runs `build(work)` in a fresh folder beside `finalDir`, then puts that folder at `finalDir`. If the build
 * throws, `finalDir` is left as it was (absent, or the earlier payload) and the work folder is removed.
 * Work folders that an earlier run was killed before it could remove are deleted first; those of a run that is
 * still going are left alone.
 */
export async function buildAtomically(finalDir, build) {
  const parent = dirname(finalDir);
  const name = basename(finalDir);
  mkdirSync(parent, { recursive: true });
  for (const entry of readdirSync(parent)) {
    const ours = entry.startsWith(`.build-${name}-`) || entry.startsWith(`.old-${name}-`);
    if (ours && isAbandoned(entry)) rmSync(join(parent, entry), { recursive: true, force: true });
  }

  const id = `${process.pid}-${randomBytes(4).toString("hex")}`;
  const work = join(parent, `.build-${name}-${id}`);
  const old = join(parent, `.old-${name}-${id}`);
  mkdirSync(work, { mode: 0o755 });
  const forget = onInterrupt(() => rmSync(work, { recursive: true, force: true }));
  try {
    await build(work);
    if (existsSync(finalDir)) renameSync(finalDir, old);
    try {
      renameSync(work, finalDir);
    } catch (err) {
      if (existsSync(old)) renameSync(old, finalDir);
      throw err;
    }
    rmSync(old, { recursive: true, force: true });
  } catch (err) {
    rmSync(work, { recursive: true, force: true });
    throw err;
  } finally {
    forget();
  }
}

/** The line of a failed program's output that says why: its last "error" line, plus the "cause" line after it. */
export function failureReason(output) {
  const lines = output
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const at = lines.findLastIndex((line) => /^error\b/i.test(line));
  if (at < 0) return lines.at(-1) ?? "no output";
  const cause = /^(cause|caused by):/i.test(lines[at + 1] ?? "") ? `; ${lines[at + 1]}` : "";
  return lines[at] + cause;
}

/** Runs a program with an argument array (never a shell) and returns its output; a failure is a BuildError. */
function run(command, args, { cwd, env, input, what = command } = {}) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, { cwd, env, stdio: [input === undefined ? "ignore" : "pipe", "pipe", "pipe"] });
    children.add(child);
    let stdout = "";
    let stderr = "";
    child.stdout.setEncoding("utf8").on("data", (text) => (stdout += text));
    child.stderr.setEncoding("utf8").on("data", (text) => (stderr += text));
    if (input !== undefined) {
      child.stdin.on("error", () => {}); // the program may exit before it has read everything
      child.stdin.end(input);
    }
    child.on("error", (err) => {
      children.delete(child);
      reject(
        new BuildError(
          err.code === "ENOENT" ? `${command} was not found on PATH` : `cannot run ${command}: ${err.message}`,
        ),
      );
    });
    child.on("close", (status, signal) => {
      children.delete(child);
      if (status === 0) return resolvePromise({ stdout, stderr });
      const how = signal ? `signal ${signal}` : `exit ${status}`;
      reject(new BuildError(`${what} failed (${how}): ${failureReason(stderr || stdout)}`));
    });
  });
}

/** The locked runtime dependencies as requirements.txt text, with their hashes. */
async function exportRequirements() {
  // --frozen: use uv.lock as it is, never re-resolve; hashes are part of the output unless --no-hashes is given.
  const args = ["export", "--frozen", "--no-dev", "--no-emit-project", "--format", "requirements.txt"];
  args.push("--no-header", "--no-annotate");
  const { stdout } = await run("uv", args, { cwd: repoRoot, what: "uv export" });
  return stdout;
}

async function extract(archive, work) {
  await run("tar", ["-xzf", archive, "--strip-components=1", "-C", work], { what: "tar" });
}

/** The CPython version the archive holds, from its headers (read before include/ is pruned). */
function readPythonVersion(work) {
  const header = readFileSync(join(work, "include", `python${PYTHON_MINOR}`, "patchlevel.h"), "utf8");
  const version = /^#define\s+PY_VERSION\s+"([^"]+)"/m.exec(header)?.[1];
  if (version !== PYTHON_VERSION)
    throw new BuildError(`the archive holds CPython ${version}, expected ${PYTHON_VERSION}`);
  return version;
}

async function installDependencies(target, work, requirements) {
  const { triple, host } = TARGETS[target];
  const site = join(work, "lib", `python${PYTHON_MINOR}`, "site-packages");
  const args = [
    "pip",
    "install",
    "--link-mode",
    "copy",
    "--target",
    site,
    "--python-platform",
    triple,
    "--python-version",
    PYTHON_MINOR,
    "--only-binary",
    ":all:",
    "--require-hashes",
    "--no-progress",
    "--no-python-downloads",
    "-r",
    "-",
  ];
  // uv needs some interpreter to ask about the host. The target's own is used when this machine can run it, so a
  // Mac with only Node and uv installed can build its own architecture; for the other targets uv finds one on the
  // machine (UV_PYTHON picks it). uv may not download one: with no interpreter at all it would fetch a CPython
  // that nothing here pins.
  const native = host.platform === process.platform && host.arch === process.arch;
  if (native) args.push("--python", join(work, "bin", `python${PYTHON_MINOR}`));
  // Run from the temp folder, not from inside the repository: uv would read the repository's pyproject.toml
  // settings and look for its .venv there. PYTHONDONTWRITEBYTECODE keeps uv's query of the payload's own
  // interpreter from writing .pyc files into the payload.
  const env = { ...process.env, PYTHONDONTWRITEBYTECODE: "1" };
  try {
    await run("uv", args, { cwd: tmpdir(), env, input: requirements, what: "uv pip install" });
  } catch (err) {
    if (!(err instanceof BuildError) || !/No interpreter found/.test(err.message)) throw err;
    throw new BuildError(`${err.message}; install a Python 3 or set UV_PYTHON to one (uv may not download one here)`);
  }
  return readdirSync(site).filter((entry) => entry.endsWith(".dist-info") && !entry.startsWith("pip-")).length;
}

/** Copies app/aihub_web into site-packages, without bytecode. Returns the number of files copied. */
function copyApp(work) {
  const source = join(repoRoot, "app", "aihub_web");
  const dest = join(work, "lib", `python${PYTHON_MINOR}`, "site-packages", "aihub_web");
  const wanted = (path) => basename(path) !== "__pycache__" && !path.endsWith(".pyc");
  cpSync(source, dest, { recursive: true, force: false, errorOnExist: true, filter: wanted });
  return measure(dest).files;
}

function measure(root) {
  let files = 0;
  let symlinks = 0;
  let bytes = 0;
  for (const { rel, dirent } of walk(root)) {
    if (dirent.isSymbolicLink()) symlinks += 1;
    else if (dirent.isFile()) {
      files += 1;
      bytes += lstatSync(join(root, rel)).size;
    }
  }
  return { files, symlinks, bytes };
}

const log = (target, message) => console.log(`build-backend: ${target}: ${message}`);
const mib = (bytes) => `${(bytes / 1048576).toFixed(1)} MiB`;
const seconds = (since) => `${((Date.now() - since) / 1000).toFixed(1)} s`;
const count = (n, noun) => `${n} ${noun}${n === 1 ? "" : "s"}`;

async function buildTarget(target) {
  const started = Date.now();
  const finalDir = join(payloadDir, target);
  const requirements = await exportRequirements();
  const archive = await fetchArchive(target);

  let python;
  await buildAtomically(finalDir, async (work) => {
    await extract(archive, work);
    python = readPythonVersion(work);
    const packages = await installDependencies(target, work, requirements);
    log(target, `installed ${packages} locked packages for ${TARGETS[target].triple}`);
    log(target, `copied aihub_web (${copyApp(work)} files)`);
    const pruned = prune(work);
    log(target, `pruned ${pruned.entries} entries (${mib(pruned.bytes)})`);
    auditPayload(work, requiredFiles(target));
  });

  const { files, symlinks, bytes } = measure(finalDir);
  const days = pbsAgeDays();
  console.log(`target   ${target} (${TARGETS[target].triple})`);
  console.log(`python   ${python}, python-build-standalone ${PBS_TAG}, ${days} days old`);
  console.log(
    `payload  ${relative(repoRoot, finalDir)}: ${mib(bytes)}, ${count(files, "file")}, ${count(symlinks, "symlink")}`,
  );
  console.log(`built in ${seconds(started)}`);
  const warning = ageWarning(days);
  if (warning) console.error(`build-backend: warning: ${warning}`);
}

/** Builds the payload for the target in `args`. Returns the exit status. */
export async function main(args) {
  try {
    const target = parseTarget(args);
    process.umask(0o022); // the modes of what is built should not depend on the caller's umask
    exitOnInterrupt();
    await buildTarget(target);
    return 0;
  } catch (err) {
    if (err instanceof BuildError) {
      console.error(`build-backend: ${err.message}`);
      return err.status;
    }
    console.error(`build-backend: unexpected error: ${err?.stack ?? err}`);
    return 1;
  }
}

if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  process.exitCode = await main(process.argv.slice(2));
}
