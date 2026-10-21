// Packages the desktop app for this machine: desktop/dist/linux-unpacked/, a folder that runs without Node or npm.
//
//   node scripts/package-linux.mjs
//
// It takes no arguments and never uses the network. The result is Electron's own Linux distribution
// (node_modules/electron/dist) with these changes, laid out as Electron finds an app:
//
//   aihub-desktop                  the binary, renamed from electron (Electron takes app.isPackaged from the name)
//   resources/default_app.asar     removed; it is what a binary without an app of its own would open
//   resources/app/package.json     the fields of desktop/package.json that the shell reads, and aihubCommit
//   resources/app/src/             desktop/src as it is: the shell's own files and nothing else
//   resources/backend/             desktop/payload/linux-x64 as build-backend.mjs left it
//
// There is no archive and no packaging tool: the app is a plain folder. Symlinks stay symlinks and are never followed,
// modes are kept except that group and others lose the write bit, and anything that is not a file, a directory or a
// symlink stops the run.
//
// Everything is built in desktop/dist/.linux-unpacked-<pid> and checked there. Only then does it replace
// desktop/dist/linux-unpacked, so a failed or interrupted run never leaves a half-made app and the earlier one stays
// as it was. If two runs overlap, each builds a whole output; usually the later swap simply replaces the earlier
// output, and a run that loses the race gets an error from the rename and removes its old folder. A whole output is
// what ends up in place either way.
//
// It needs npm ci in desktop/ (which brings Electron's binary) and npm run build:backend. The commit string in
// package.json is the first 12 characters of HEAD, with -dirty added when git shows changes under desktop/ or app/,
// and unknown when there is no git or the checkout is not a repository of its own.
//
// Exit status: 0 on success, 2 for a bad command line or a machine that is not Linux x86_64, 1 for any other
// failure (one line on stderr says why).

import { execFileSync } from "node:child_process";
import {
  chmodSync,
  closeSync,
  constants,
  copyFileSync,
  existsSync,
  lstatSync,
  mkdirSync,
  openSync,
  readdirSync,
  readFileSync,
  readlinkSync,
  readSync,
  realpathSync,
  renameSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { basename, dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { measure, requiredFiles, TARGET } from "./build-backend.mjs";

export const OUTPUT = "linux-unpacked";
export const BINARY = "aihub-desktop";
// The fields of desktop/package.json that go into the app's package.json (aihubCommit is added to them).
export const APP_FIELDS = ["name", "productName", "version", "desktopName", "main", "type"];

const desktopDir = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = resolve(desktopDir, "..");

/** A failure with a reason worth one line; `status` is the process exit status (2 for a bad command line). */
export class PackageError extends Error {
  constructor(message, status = 1) {
    super(message);
    this.name = "PackageError";
    this.status = status;
  }
}

// ---------------------------------------------------------------------------------------------------------------
// The command line, the host and the inputs

/** Throws a PackageError with status 2 and the usage line unless the command line is empty. */
export function checkArguments(args) {
  if (args.length > 0) throw new PackageError("usage: node scripts/package-linux.mjs (it takes no arguments)", 2);
}

/** Throws a PackageError with status 2 unless this machine is Linux on an x86-64 CPU. */
export function assertHost(platform = process.platform, arch = process.arch) {
  if (platform === "linux" && arch === "x64") return;
  throw new PackageError(`must run on Linux x86_64, not ${platform} ${arch}`, 2);
}

const ELF_MAGIC = Buffer.from([0x7f, 0x45, 0x4c, 0x46]);

function isElf(path) {
  let fd;
  try {
    fd = openSync(path, "r");
    const head = Buffer.alloc(ELF_MAGIC.length);
    return readSync(fd, head, 0, head.length, 0) === head.length && head.equals(ELF_MAGIC);
  } catch {
    return false;
  } finally {
    if (fd !== undefined) closeSync(fd);
  }
}

const isExecutable = (path) => (statSync(path).mode & 0o111) !== 0;

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch (err) {
    if (err?.code === "ENOENT") return undefined;
    throw new PackageError(`cannot read ${basename(path)}: ${String(err.message).split("\n")[0]}`);
  }
}

/**
 * Checks the Electron distribution that npm ci leaves: the version pinned in desktop/package.json, the installed
 * package and the version file of the distribution all agree, and dist/electron is an executable ELF file. Returns
 * what the build needs.
 */
export function checkElectron(desktop = desktopDir) {
  const pkg = readJson(join(desktop, "package.json"));
  if (pkg === undefined) throw new PackageError("desktop/package.json is missing");
  const pinned = pkg.devDependencies?.electron;
  if (typeof pinned !== "string") throw new PackageError("package.json must pin electron in devDependencies");
  if (!/^\d+\.\d+\.\d+$/.test(pinned)) {
    throw new PackageError(`devDependencies.electron must be an exact version, not ${pinned}`);
  }
  const fix = "run npm ci in desktop/";
  // npm ci installs the package but not the binary: node_modules/electron/install.js fetches it (npm start runs it).
  const install = "run node node_modules/electron/install.js in desktop/";
  const root = join(desktop, "node_modules", "electron");
  const installed = readJson(join(root, "package.json"))?.version;
  if (installed === undefined) throw new PackageError(`node_modules/electron is not installed: ${fix}`);
  if (installed !== pinned) {
    throw new PackageError(`node_modules/electron is ${installed} but package.json pins ${pinned}: ${fix}`);
  }
  const dist = join(root, "dist");
  const binary = join(dist, "electron");
  if (!existsSync(binary)) {
    throw new PackageError(`node_modules/electron/dist/electron is missing: ${install} (npm ci does not fetch it)`);
  }
  let shipped;
  try {
    // Electron's own install.js ignores a leading v in this file, so this does too.
    shipped = readFileSync(join(dist, "version"), "utf8").trim().replace(/^v/, "");
  } catch {
    throw new PackageError(`node_modules/electron/dist/version is missing: ${install}`);
  }
  if (shipped !== pinned) {
    throw new PackageError(`node_modules/electron/dist/version says ${shipped}, not ${pinned}: ${install}`);
  }
  // install.js counts a distribution with the right version file as installed, so a damaged binary has to go first.
  const repair = `remove node_modules/electron/dist, then ${install}`;
  if (!isExecutable(binary)) throw new PackageError(`node_modules/electron/dist/electron is not executable: ${repair}`);
  if (!isElf(binary)) throw new PackageError(`node_modules/electron/dist/electron is not an ELF binary: ${repair}`);
  return { pkg, version: pinned, dist };
}

/** Throws unless desktop/payload/linux-x64 has what build-backend.mjs always leaves, with a real interpreter. */
export function checkPayload(payload = join(desktopDir, "payload", TARGET)) {
  const name = `payload/${TARGET}`;
  const fix = "run npm run build:backend in desktop/";
  if (!existsSync(payload)) throw new PackageError(`${name} is missing: ${fix}`);
  for (const rel of requiredFiles()) {
    if (!existsSync(join(payload, rel))) throw new PackageError(`${name} is incomplete (missing ${rel}): ${fix}`);
  }
  const interpreter = requiredFiles().find((rel) => /^bin\/python3\.\d+$/.test(rel));
  if (!isExecutable(join(payload, interpreter))) throw new PackageError(`${name}/${interpreter} is not executable`);
  if (!isElf(join(payload, interpreter))) throw new PackageError(`${name}/${interpreter} is not an ELF binary`);
}

/** Throws when the payload's aihub_web differs from app/aihub_web: the backend would not be the code at HEAD. */
export function checkPayloadIsCurrent(
  source = join(repoRoot, "app", "aihub_web"),
  payload = join(desktopDir, "payload", TARGET),
) {
  if (!existsSync(source)) return;
  const copy = join(payload, "lib", "python3.12", "site-packages", "aihub_web");
  const files = (root) => {
    const out = new Map();
    const visit = (rel) => {
      for (const entry of readdirSync(join(root, rel), { withFileTypes: true })) {
        const path = rel ? `${rel}/${entry.name}` : entry.name;
        if (entry.name === "__pycache__" || path.endsWith(".pyc")) continue;
        if (entry.isDirectory()) visit(path);
        else out.set(path, readFileSync(join(root, path)));
      }
    };
    visit("");
    return out;
  };
  const want = files(source);
  const have = existsSync(copy) ? files(copy) : new Map();
  const differ = [...want.keys(), ...have.keys()].filter(
    (path) => !want.get(path)?.equals(have.get(path) ?? Buffer.alloc(0)) || want.has(path) !== have.has(path),
  );
  if (differ.length) {
    throw new PackageError(
      `payload/${TARGET} holds a different aihub_web than app/ (${differ[0]}): run npm run build:backend in desktop/`,
    );
  }
}

/** The package.json of the packaged app: the named fields of `pkg`, then the commit string. */
export function appPackageJson(pkg, commit) {
  const app = {};
  for (const field of APP_FIELDS) {
    if (typeof pkg[field] !== "string" || pkg[field] === "") throw new PackageError(`package.json has no ${field}`);
    app[field] = pkg[field];
  }
  return { ...app, aihubCommit: commit };
}

function runGit(repo) {
  return (args) =>
    // No GIT_* variables (GIT_DIR would point git at another repository), no optional index write, no fsmonitor.
    execFileSync("git", ["--no-optional-locks", "-c", "core.fsmonitor=false", "-C", repo, ...args], {
      encoding: "utf8",
      stdio: ["ignore", "pipe", "ignore"],
      timeout: 10_000,
      env: Object.fromEntries(Object.entries(process.env).filter(([name]) => !name.startsWith("GIT_"))),
    });
}

function realOrResolved(path) {
  try {
    return realpathSync(path);
  } catch {
    return resolve(path);
  }
}

/**
 * HEAD's first 12 characters, with -dirty when git lists a change under desktop/ or app/, or in uv.lock or pyproject.toml (the payload is built from them) (files that git ignores,
 * such as desktop/dist, do not count), or unknown when there is no git, the directory is not a repository of its
 * own (a parent's HEAD would be the wrong answer), or git answers with something that is not a hash.
 */
export function commitString(repo = repoRoot, run = runGit(repo)) {
  try {
    if (realOrResolved(run(["rev-parse", "--show-toplevel"]).trim()) !== realOrResolved(repo)) return "unknown";
    const head = run(["rev-parse", "HEAD"]).trim();
    if (!/^[0-9a-f]{40,64}$/.test(head)) return "unknown";
    const dirty =
      run([
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        "desktop",
        "app",
        "uv.lock",
        "pyproject.toml",
      ]).trim() !== "";
    return head.slice(0, 12) + (dirty ? "-dirty" : "");
  } catch {
    return "unknown";
  }
}

// ---------------------------------------------------------------------------------------------------------------
// Copying and checking a tree

const byName = (a, b) => (a.name < b.name ? -1 : 1);
// The mode of a copy: that of the original, without the write bit for group and others.
const copyMode = (mode) => mode & 0o7777 & ~0o022;
// A directory keeps the owner's write access, or the script could not remove its own output (rmSync does not chmod).
const dirMode = (mode) => copyMode(mode) | 0o700;

function kindOf(stat) {
  if (stat.isFIFO()) return "FIFO";
  if (stat.isSocket()) return "socket";
  if (stat.isCharacterDevice()) return "character device";
  if (stat.isBlockDevice()) return "block device";
  return "unknown type";
}

/**
 * Copies the directory `src` to `dest` (which must not exist, or be an empty directory), entry by entry. A symlink is
 * copied as the link it is and never followed. A file or directory keeps its mode, minus the write bit for group and
 * others, whatever the umask. Anything that is not a file, a directory or a symlink throws.
 */
export function copyTree(src, dest) {
  const mode = dirMode(lstatSync(src).mode);
  if (existsSync(dest)) {
    if (readdirSync(dest).length > 0) throw new PackageError(`${dest} is not empty`);
  } else {
    mkdirSync(dest, { mode: 0o700 });
  }
  chmodSync(dest, 0o700); // owner-only while it is filled, so that a read-only directory can still receive its files
  copyEntries(src, dest, "");
  chmodSync(dest, mode);
}

function copyEntries(src, dest, rel) {
  for (const entry of readdirSync(join(src, rel), { withFileTypes: true }).sort(byName)) {
    const path = rel ? `${rel}/${entry.name}` : entry.name;
    const from = join(src, path);
    const to = join(dest, path);
    const stat = lstatSync(from);
    if (stat.isSymbolicLink()) {
      symlinkSync(readlinkSync(from), to);
    } else if (stat.isDirectory()) {
      mkdirSync(to, { mode: 0o700 });
      copyEntries(src, dest, path);
      chmodSync(to, dirMode(stat.mode));
    } else if (stat.isFile()) {
      copyFileSync(from, to, constants.COPYFILE_EXCL);
      chmodSync(to, copyMode(stat.mode));
    } else {
      throw new PackageError(`${path}: not a regular file, directory or symlink (${kindOf(stat)})`);
    }
  }
}

/** Every entry below `root`, sorted, parents before children, without following symlinks. */
function* walk(root, rel = "") {
  for (const entry of readdirSync(join(root, rel), { withFileTypes: true }).sort(byName)) {
    const path = rel ? `${rel}/${entry.name}` : entry.name;
    yield { rel: path, stat: lstatSync(join(root, path)) };
    if (entry.isDirectory()) yield* walk(root, path);
  }
}

const inside = (root, path) => path === root || path.startsWith(root + sep);

/**
 * Throws unless the finished output is what it should be: nothing that group or others can write, no file that is not
 * a file, a directory or a symlink, and no symlink that is absolute, dangling or leads out of the output (by its
 * own path or through other links).
 */
export function auditOutput(root) {
  const base = resolve(root);
  const realBase = realpathSync.native(base);
  const problems = [];
  if (lstatSync(base).mode & 0o022) problems.push("writable by group or others: the output folder itself");
  for (const { rel, stat } of walk(base)) {
    const full = join(base, rel);
    if (stat.isSymbolicLink()) {
      const link = readlinkSync(full);
      if (isAbsolute(link)) {
        problems.push(`absolute symlink: ${rel} -> ${link}`);
      } else if (!inside(base, resolve(dirname(full), link))) {
        problems.push(`symlink leaves the output: ${rel} -> ${link}`);
      } else {
        let real;
        try {
          real = realpathSync.native(full);
        } catch {
          problems.push(`dangling symlink: ${rel} -> ${link}`);
        }
        if (real !== undefined && !inside(realBase, real))
          problems.push(`symlink leaves the output: ${rel} -> ${link}`);
      }
    } else if (!stat.isFile() && !stat.isDirectory()) {
      problems.push(`not a regular file, directory or symlink: ${rel}`);
    } else if (stat.mode & 0o022) {
      problems.push(`writable by group or others: ${rel}`);
    }
  }
  if (problems.length) {
    const more = problems.length > 5 ? ` (and ${problems.length - 5} more)` : "";
    throw new PackageError(`output check failed: ${problems.slice(0, 5).join("; ")}${more}`);
  }
}

// ---------------------------------------------------------------------------------------------------------------
// Building in a folder beside the output

const cleanups = new Set();

/** Registers what to undo if the run is interrupted; returns the function that forgets it. */
export function onInterrupt(cleanup) {
  cleanups.add(cleanup);
  return () => cleanups.delete(cleanup);
}

/** SIGINT and SIGTERM run the registered cleanups and end the run with the usual status (130, 143). */
export function exitOnInterrupt() {
  for (const [signal, status] of [
    ["SIGINT", 130],
    ["SIGTERM", 143],
  ]) {
    process.once(signal, () => {
      for (const cleanup of cleanups) cleanup();
      process.exit(status);
    });
  }
}

// The steps of a run are synchronous. Between them the event loop gets a turn, which is when a signal that arrived
// during a step is handled, so that an interrupted run stops before the next step and never before the swap it has
// already started.
const tick = () => new Promise((resolve) => setImmediate(resolve));

/** The pid in the name of a leftover of this script, .linux-unpacked-<pid> or .old-linux-unpacked-<pid>, else null. */
function leftoverPid(entry, name) {
  const match = /^\.(?:old-)?(.+)-(\d+)$/.exec(entry);
  return match !== null && match[1] === name ? Number(match[2]) : null;
}

/** Whether the process with this pid is gone. */
function isGone(pid) {
  try {
    process.kill(pid, 0);
    return false;
  } catch (err) {
    return err.code !== "EPERM"; // EPERM: the process exists but belongs to someone else
  }
}

const present = (path) => {
  try {
    lstatSync(path);
    return true;
  } catch {
    return false;
  }
};

/**
 * Runs `build(work)` in a fresh folder beside `finalDir`, then puts that folder at `finalDir`. If the build throws, or
 * the run is interrupted, `finalDir` is left as it was (absent, or the earlier output) and the folder is removed.
 * Folders that an earlier run was killed before it could remove are deleted first; those of a run that is still going
 * are left alone.
 */
export async function replaceOutput(finalDir, build) {
  const parent = dirname(finalDir);
  const name = basename(finalDir);
  mkdirSync(parent, { recursive: true });
  // A run killed between its two renames left the earlier output as .old-<name>-<pid>: put it back first.
  for (const entry of readdirSync(parent)) {
    const pid = leftoverPid(entry, name);
    if (pid !== null && entry.startsWith(".old-") && isGone(pid) && !present(finalDir))
      renameSync(join(parent, entry), finalDir);
  }
  for (const entry of readdirSync(parent)) {
    // Only an exact .linux-unpacked-<pid> is ours; any other name in dist/ belongs to someone else.
    const pid = leftoverPid(entry, name);
    if (pid !== null && isGone(pid)) rmSync(join(parent, entry), { recursive: true, force: true });
  }

  const work = join(parent, `.${name}-${process.pid}`);
  const old = join(parent, `.old-${name}-${process.pid}`);
  // Under this pid, so left by an earlier run whose pid has been used again.
  rmSync(work, { recursive: true, force: true });
  rmSync(old, { recursive: true, force: true });
  mkdirSync(work, { mode: 0o755 });
  const forget = onInterrupt(() => rmSync(work, { recursive: true, force: true }));
  try {
    await build(work);
    await tick();
    const replaced = present(finalDir);
    if (replaced) renameSync(finalDir, old);
    try {
      renameSync(work, finalDir);
    } catch (err) {
      if (replaced && !present(finalDir)) renameSync(old, finalDir);
      else rmSync(old, { recursive: true, force: true }); // another run put its output there; ours is not needed
      throw err;
    }
    rmSync(old, { recursive: true, force: true });
  } finally {
    rmSync(work, { recursive: true, force: true });
    forget();
  }
}

/** Throws unless the finished output has its binary, the shell's main file and the payload's required files. */
export function checkComplete(root, pkg) {
  if (!isElf(join(root, BINARY))) throw new PackageError(`output check failed: ${BINARY} is not an ELF binary`);
  if (!existsSync(join(root, "resources", "app", pkg.main))) {
    throw new PackageError(`output check failed: resources/app/${pkg.main} is missing (is desktop/src empty?)`);
  }
  for (const rel of requiredFiles()) {
    if (!existsSync(join(root, "resources", "backend", rel))) {
      throw new PackageError(`output check failed: resources/backend/${rel} is missing`);
    }
  }
}

/** Fills the empty folder `work` with the unpacked app. */
async function assemble(work, { desktop, dist, pkg, commit }) {
  copyTree(dist, work);
  await tick();
  renameSync(join(work, "electron"), join(work, BINARY));
  rmSync(join(work, "resources", "default_app.asar"), { force: true });
  await tick();

  const app = join(work, "resources", "app");
  if (present(app)) throw new PackageError("the Electron distribution already has resources/app");
  mkdirSync(join(work, "resources"), { recursive: true });
  mkdirSync(app, { mode: 0o755 });
  chmodSync(app, 0o755);
  writeFileSync(join(app, "package.json"), `${JSON.stringify(appPackageJson(pkg, commit), null, 2)}\n`, {
    mode: 0o644,
  });
  copyTree(join(desktop, "src"), join(app, "src"));
  await tick();

  copyTree(join(desktop, "payload", TARGET), join(work, "resources", "backend"));
  await tick();
  auditOutput(work);
  checkComplete(work, pkg);
}

const mib = (bytes) => `${(bytes / 1048576).toFixed(1)} MiB`;
const count = (n, noun) => `${n} ${noun}${n === 1 ? "" : "s"}`;

async function run() {
  const { pkg, version, dist } = checkElectron();
  checkPayload();
  checkPayloadIsCurrent();
  const commit = commitString();
  const finalDir = join(desktopDir, "dist", OUTPUT);
  await replaceOutput(finalDir, (work) => assemble(work, { desktop: desktopDir, dist, pkg, commit }));

  const { files, symlinks, bytes } = measure(finalDir);
  console.log(`output   ${relative(repoRoot, finalDir)}`);
  console.log(`size     ${mib(bytes)}, ${count(files, "file")}, ${count(symlinks, "symlink")}`);
  console.log(`commit   ${commit}`);
  console.log(`electron ${version}`);
}

/** Builds the unpacked app; `args` must be empty. Returns the exit status. */
export async function main(args) {
  try {
    checkArguments(args);
    assertHost();
    process.umask(0o022); // the modes of what is built should not depend on the caller's umask
    exitOnInterrupt();
    await run();
    return 0;
  } catch (err) {
    if (err instanceof PackageError) {
      console.error(`package-linux: ${err.message}`);
      return err.status;
    }
    if (typeof err?.code === "string" && /^E[A-Z0-9]+$/.test(err.code)) {
      console.error(`package-linux: ${String(err.message).split("\n")[0]}`);
      return 1;
    }
    console.error(`package-linux: unexpected error: ${err?.stack ?? err}`);
    return 1;
  }
}

if (process.argv[1] && realpathSync(process.argv[1]) === realpathSync(fileURLToPath(import.meta.url))) {
  process.exitCode = await main(process.argv.slice(2));
}
