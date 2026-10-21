// scripts/package-linux.mjs without the real Electron or the real payload: its pure helpers, and the whole script run
// as a child process on a small fake desktop/ tree (a stand-in Electron distribution, payload and src).

import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  copyFileSync,
  cpSync,
  existsSync,
  lstatSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  renameSync,
  readFileSync,
  readlinkSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { afterEach, before, beforeEach, describe, it } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import { requiredFiles } from "../scripts/build-backend.mjs";
import {
  APP_FIELDS,
  BINARY,
  OUTPUT,
  PackageError,
  appPackageJson,
  assertHost,
  auditOutput,
  checkArguments,
  checkElectron,
  checkPayload,
  commitString,
  copyTree,
  replaceOutput,
} from "../scripts/package-linux.mjs";

const scripts = join(dirname(fileURLToPath(import.meta.url)), "..", "scripts");
const realScript = join(scripts, "package-linux.mjs");

// The modes of the fixtures should not depend on the umask of whoever runs the tests.
before(() => {
  process.umask(0o022);
});

let scratch;
beforeEach(() => {
  scratch = mkdtempSync(join(tmpdir(), "package-linux-test-"));
});
afterEach(() => {
  rmSync(scratch, { recursive: true, force: true });
});

const ELF = Buffer.from([0x7f, 0x45, 0x4c, 0x46, 2, 1, 1, 0]);
const sha = (path) => createHash("sha256").update(readFileSync(path)).digest("hex");
const octal = (mode) => (mode & 0o7777).toString(8);
const byName = (a, b) => (a.name < b.name ? -1 : 1);

function put(root, rel, content = `contents of ${rel}\n`, mode = 0o644) {
  const full = join(root, rel);
  mkdirSync(dirname(full), { recursive: true });
  writeFileSync(full, content);
  chmodSync(full, mode);
}

/** Every entry below root as sorted lines: path, mode and sha256 of a file, mode of a directory, target of a link. */
function snapshot(root, rel = "") {
  const lines = [];
  for (const entry of readdirSync(join(root, rel), { withFileTypes: true }).sort(byName)) {
    const path = rel ? `${rel}/${entry.name}` : entry.name;
    const full = join(root, path);
    if (entry.isSymbolicLink()) lines.push(`${path} -> ${readlinkSync(full)}`);
    else if (entry.isDirectory()) lines.push(`${path}/ ${octal(lstatSync(full).mode)}`, ...snapshot(root, path));
    else lines.push(`${path} ${octal(lstatSync(full).mode)} ${sha(full)}`);
  }
  return lines;
}

const PINNED = "44.6.0";
const APP_PACKAGE = {
  name: "aihub-desktop",
  productName: "aihub",
  version: "0.1.0",
  private: true,
  desktopName: "dev.akhil-p.aihub.desktop",
  main: "src/main.js",
  type: "module",
  scripts: { test: "node --test" },
  devDependencies: { electron: PINNED },
};

/** A fake Electron distribution: the binary, the version file, the default app, a pak file and a library link. */
function layElectron(desktop, version = PINNED) {
  const pkg = join(desktop, "node_modules", "electron");
  put(pkg, "package.json", JSON.stringify({ name: "electron", version }));
  const dist = join(pkg, "dist");
  put(dist, "electron", Buffer.concat([ELF, Buffer.from("the browser")]), 0o755);
  put(dist, "version", version);
  put(dist, "chrome-sandbox", Buffer.concat([ELF, Buffer.from("sandbox")]), 0o755);
  put(dist, "resources/default_app.asar", "the default app");
  put(dist, "resources.pak");
  put(dist, "locales/en-US.pak");
  put(dist, "libfoo.so.1", ELF);
  symlinkSync("libfoo.so.1", join(dist, "libfoo.so"));
  return dist;
}

/** A fake payload with the files that build-backend.mjs always leaves, and a link and a script besides. */
function layPayload(desktop) {
  const payload = join(desktop, "payload", "linux-x64");
  for (const rel of requiredFiles()) {
    const interpreter = rel === "bin/python3.12";
    put(
      payload,
      rel,
      interpreter ? Buffer.concat([ELF, Buffer.from("python")]) : `contents of ${rel}\n`,
      interpreter ? 0o755 : 0o644,
    );
  }
  symlinkSync("python3.12", join(payload, "bin", "python3"));
  put(payload, "bin/pip-stand-in", "#!/bin/sh\n", 0o755);
  return payload;
}

/** A fake desktop/ with the two scripts copied in, so that running the copy finds the fake tree beside it. */
function fakeDesktop(root = join(scratch, "tree")) {
  const desktop = join(root, "desktop");
  put(desktop, "package.json", `${JSON.stringify(APP_PACKAGE, null, 2)}\n`);
  put(desktop, "package-lock.json", "{}\n");
  put(desktop, ".npmrc", "a=b\n");
  for (const rel of ["main.js", "backend.js", "loading.html", "deep/er/module.js"]) put(desktop, `src/${rel}`);
  put(desktop, "test/x.test.mjs");
  mkdirSync(join(desktop, "scripts"), { recursive: true });
  for (const name of ["package-linux.mjs", "build-backend.mjs"])
    copyFileSync(join(scripts, name), join(desktop, "scripts", name));
  layElectron(desktop);
  layPayload(desktop);
  return desktop;
}

const outputOf = (desktop) => join(desktop, "dist", OUTPUT);

/** Runs the copy of the script that sits in `desktop`. `prefix` is a command to run it under, for example unshare. */
function runIn(desktop, args = [], { prefix = [], env = {} } = {}) {
  const argv = [...prefix, process.execPath, join(desktop, "scripts", "package-linux.mjs"), ...args];
  return spawnSync(argv[0], argv.slice(1), {
    encoding: "utf8",
    cwd: dirname(desktop),
    env: { PATH: process.env.PATH, HOME: scratch, ...env },
  });
}

function git(cwd, ...args) {
  const run = spawnSync("git", args, {
    cwd,
    encoding: "utf8",
    env: {
      PATH: process.env.PATH,
      HOME: scratch,
      GIT_CONFIG_NOSYSTEM: "1",
      GIT_AUTHOR_NAME: "t",
      GIT_AUTHOR_EMAIL: "t@t",
      GIT_COMMITTER_NAME: "t",
      GIT_COMMITTER_EMAIL: "t@t",
    },
  });
  assert.equal(run.status, 0, run.stderr);
  return run.stdout.trim();
}

/** Makes the parent of the fake desktop/ a git repository with one commit. Returns the full hash. */
function gitInit(root) {
  put(root, ".gitignore", "/desktop/dist/\n/desktop/payload/\n/desktop/node_modules/\n");
  cpSync(join(root, "desktop/payload/linux-x64/lib/python3.12/site-packages/aihub_web"), join(root, "app/aihub_web"), {
    recursive: true,
  });
  put(root, "README.md");
  git(root, "init", "-q");
  git(root, "add", ".");
  git(root, "commit", "-q", "-m", "first");
  return git(root, "rev-parse", "HEAD");
}

describe("the command line and the host", () => {
  it("takes no arguments", () => {
    assert.doesNotThrow(() => checkArguments([]));
  });

  it("refuses any argument with one usage line and status 2", () => {
    assert.throws(
      () => checkArguments(["linux"]),
      (err) =>
        err instanceof PackageError &&
        err.status === 2 &&
        /^usage: node scripts\/package-linux\.mjs \(it takes no arguments\)$/.test(err.message),
    );
  });

  it("runs on Linux x86_64 and refuses everything else with status 2", () => {
    assert.doesNotThrow(() => assertHost("linux", "x64"));
    for (const [platform, arch] of [
      ["freebsd", "x64"],
      ["linux", "arm64"],
      ["win32", "x64"],
    ]) {
      assert.throws(
        () => assertHost(platform, arch),
        (err) => err.status === 2 && err.message.includes(`${platform} ${arch}`) && !err.message.includes("\n"),
      );
    }
  });
});

describe("the package.json of the packaged app", () => {
  it("keeps only the named fields, in order, and adds the commit", () => {
    const app = appPackageJson(APP_PACKAGE, "abc123abc123");
    assert.deepEqual(Object.keys(app), [...APP_FIELDS, "aihubCommit"]);
    assert.deepEqual(APP_FIELDS, ["name", "productName", "version", "desktopName", "main", "type"]);
    assert.equal(app.aihubCommit, "abc123abc123");
    assert.equal(app.main, "src/main.js");
    assert.equal(app.scripts, undefined);
    assert.equal(app.devDependencies, undefined);
    assert.equal(app.private, undefined);
  });

  it("names the field that is missing", () => {
    const { desktopName, ...without } = APP_PACKAGE;
    assert.throws(() => appPackageJson(without, "x"), /package\.json has no desktopName/);
    assert.throws(() => appPackageJson({ ...APP_PACKAGE, version: "" }, "x"), /package\.json has no version/);
  });

  it("works on the real desktop/package.json, whose main file is in src/", () => {
    const real = JSON.parse(readFileSync(join(scripts, "..", "package.json"), "utf8"));
    const app = appPackageJson(real, "x");
    assert.equal(app.type, "module");
    assert.ok(existsSync(join(scripts, "..", app.main)), `${app.main} should exist`);
    assert.ok(app.main.startsWith("src/"));
  });
});

describe("the commit string", () => {
  const HASH = "0123456789abcdef0123456789abcdef01234567";
  const answers =
    ({ top = "/repo", head = HASH, status = "" } = {}) =>
    (args) => {
      const key = args.join(" ");
      if (key === "rev-parse --show-toplevel") return `${top}\n`;
      if (key === "rev-parse HEAD") return `${head}\n`;
      if (key === "status --porcelain --untracked-files=all -- desktop app uv.lock pyproject.toml") return status;
      throw new Error(`unexpected git call: ${key}`);
    };

  it("is the first 12 characters of HEAD when desktop/ and app/ are clean", () => {
    assert.equal(commitString("/repo", answers()), "0123456789ab");
  });

  it("gets -dirty when git reports a change under desktop/ or app/", () => {
    assert.equal(commitString("/repo", answers({ status: " M desktop/src/main.js\n" })), "0123456789ab-dirty");
  });

  it("is unknown without git, or when git is not about this repository", () => {
    assert.equal(
      commitString("/repo", () => {
        throw new Error("spawn git ENOENT");
      }),
      "unknown",
    );
    assert.equal(commitString("/repo", answers({ top: "/somewhere/else" })), "unknown");
    assert.equal(commitString("/repo", answers({ head: "not a hash" })), "unknown");
  });

  it("works with real git: clean, changed, untracked, ignored output, a change elsewhere, no repository, a parent repository", () => {
    const root = join(scratch, "repo");
    fakeDesktop(root);
    const full = gitInit(root);
    assert.equal(commitString(root), full.slice(0, 12));
    put(root, "desktop/dist/linux-unpacked/aihub-desktop", "ignored output");
    put(root, "README.md", "changed\n");
    assert.equal(
      commitString(root),
      full.slice(0, 12),
      "ignored output and a change outside desktop/ and app/ do not count",
    );
    put(root, "desktop/src/main.js", "changed\n");
    assert.equal(commitString(root), `${full.slice(0, 12)}-dirty`);
    git(root, "checkout", "--", "desktop/src/main.js");
    assert.equal(commitString(root), full.slice(0, 12));
    put(root, "desktop/src/new.js");
    assert.equal(commitString(root), `${full.slice(0, 12)}-dirty`, "an untracked file under desktop/ counts");

    const loose = join(scratch, "loose");
    mkdirSync(loose);
    assert.equal(commitString(loose), "unknown", "scratch is not a repository");
    const inner = join(root, "not-a-repo-root");
    mkdirSync(inner);
    assert.equal(commitString(inner), "unknown", "the parent's HEAD is not this directory's");
  });
});

describe("the Electron distribution check", () => {
  let desktop;
  beforeEach(() => {
    desktop = fakeDesktop();
  });
  const dist = () => join(desktop, "node_modules", "electron", "dist");
  const failsWith = (pattern) =>
    assert.throws(
      () => checkElectron(desktop),
      (err) =>
        err instanceof PackageError && err.status === 1 && !err.message.includes("\n") && pattern.test(err.message),
    );

  it("accepts the pinned version with its binary", () => {
    const found = checkElectron(desktop);
    assert.equal(found.version, PINNED);
    assert.equal(found.dist, dist());
  });

  it("tells to run npm ci when the package is not installed", () => {
    rmSync(join(desktop, "node_modules", "electron"), { recursive: true });
    failsWith(/node_modules\/electron is not installed: run npm ci in desktop\//);
  });

  it("refuses an installed version other than the pin", () => {
    writeFileSync(join(desktop, "node_modules", "electron", "package.json"), JSON.stringify({ version: "44.5.1" }));
    failsWith(/is 44\.5\.1 but package\.json pins 44\.6\.0: run npm ci/);
  });

  it("refuses a pin that is not an exact version", () => {
    writeFileSync(
      join(desktop, "package.json"),
      JSON.stringify({ ...APP_PACKAGE, devDependencies: { electron: "^44.6.0" } }),
    );
    failsWith(/must be an exact version, not \^44\.6\.0/);
    writeFileSync(join(desktop, "package.json"), JSON.stringify({ ...APP_PACKAGE, devDependencies: {} }));
    failsWith(/must pin electron in devDependencies/);
  });

  it("refuses a dist/version that is wrong or missing, and says how to fetch the binary", () => {
    writeFileSync(join(dist(), "version"), "44.5.1");
    failsWith(/dist\/version says 44\.5\.1, not 44\.6\.0: run node node_modules\/electron\/install\.js in desktop\//);
    rmSync(join(dist(), "version"));
    failsWith(/dist\/version is missing: run node node_modules\/electron\/install\.js in desktop\//);
  });

  it("accepts a dist/version with a leading v, as Electron's own installer does", () => {
    writeFileSync(join(dist(), "version"), `v${PINNED}\n`);
    assert.equal(checkElectron(desktop).version, PINNED);
  });

  it("refuses a missing, non-executable or non-ELF binary, and says what repairs each", () => {
    const repair =
      /: remove node_modules\/electron\/dist, then run node node_modules\/electron\/install\.js in desktop\//;
    chmodSync(join(dist(), "electron"), 0o644);
    failsWith(new RegExp(`dist/electron is not executable${repair.source}`));
    chmodSync(join(dist(), "electron"), 0o755);
    writeFileSync(join(dist(), "electron"), "#!/bin/sh\n");
    failsWith(new RegExp(`dist/electron is not an ELF binary${repair.source}`));
    rmSync(join(dist(), "electron"));
    failsWith(
      /dist\/electron is missing: run node node_modules\/electron\/install\.js in desktop\/ \(npm ci does not fetch it\)/,
    );
  });
});

describe("the payload check", () => {
  let desktop;
  let payload;
  beforeEach(() => {
    desktop = fakeDesktop();
    payload = join(desktop, "payload", "linux-x64");
  });
  const failsWith = (pattern) =>
    assert.throws(
      () => checkPayload(payload),
      (err) =>
        err instanceof PackageError && err.status === 1 && !err.message.includes("\n") && pattern.test(err.message),
    );

  it("accepts a payload with everything build-backend.mjs always leaves", () => {
    assert.doesNotThrow(() => checkPayload(payload));
  });

  it("names a missing file and says to run npm run build:backend", () => {
    for (const rel of requiredFiles()) {
      const full = join(payload, rel);
      const bytes = readFileSync(full);
      const { mode } = lstatSync(full);
      rmSync(full);
      failsWith(new RegExp(`missing ${rel.replaceAll(".", "\\.")}.*npm run build:backend`));
      writeFileSync(full, bytes);
      chmodSync(full, mode);
    }
  });

  it("refuses a missing payload folder", () => {
    rmSync(payload, { recursive: true });
    failsWith(/payload\/linux-x64 is missing: run npm run build:backend in desktop\//);
  });

  it("refuses an interpreter that is empty, not executable or not ELF", () => {
    const python = join(payload, "bin", "python3.12");
    chmodSync(python, 0o644);
    failsWith(/bin\/python3\.12 is not executable/);
    chmodSync(python, 0o755);
    writeFileSync(python, "");
    failsWith(/bin\/python3\.12 is not an ELF binary/);
    writeFileSync(python, "#!/bin/sh\n");
    failsWith(/bin\/python3\.12 is not an ELF binary/);
  });
});

describe("copying a tree", () => {
  it("keeps modes, file contents and symlinks as they are, and follows nothing", () => {
    const src = join(scratch, "src");
    put(src, "a", "a", 0o755);
    put(src, "sub/b", "b", 0o640);
    put(src, "sub/c", "c", 0o600);
    chmodSync(join(src, "sub"), 0o750);
    put(scratch, "outside/secret", "secret");
    symlinkSync("a", join(src, "rel"));
    symlinkSync("../outside/secret", join(src, "out"));
    symlinkSync(join(scratch, "outside"), join(src, "dirlink"));
    copyTree(src, join(scratch, "dst"));
    assert.deepEqual(snapshot(join(scratch, "dst")), snapshot(src));
    assert.equal(readlinkSync(join(scratch, "dst", "dirlink")), join(scratch, "outside"));
    assert.equal(lstatSync(join(scratch, "dst", "out")).isSymbolicLink(), true);
  });

  it("keeps the same modes whatever the umask of the caller", () => {
    const src = join(scratch, "src");
    put(src, "sub/b", "b", 0o755);
    chmodSync(join(src, "sub"), 0o755);
    const before = process.umask(0o077);
    try {
      copyTree(src, join(scratch, "dst"));
    } finally {
      process.umask(before);
    }
    assert.deepEqual(snapshot(join(scratch, "dst")), snapshot(src));
  });

  it("takes the write bit from group and others and nothing else", () => {
    const src = join(scratch, "src");
    put(src, "g", "g", 0o664);
    put(src, "o", "o", 0o757);
    put(src, "d/x", "x", 0o644);
    chmodSync(join(src, "d"), 0o777);
    copyTree(src, join(scratch, "dst"));
    assert.equal(octal(lstatSync(join(scratch, "dst", "g")).mode), "644");
    assert.equal(octal(lstatSync(join(scratch, "dst", "o")).mode), "755");
    assert.equal(octal(lstatSync(join(scratch, "dst", "d")).mode), "755");
  });

  it("copies a directory that is read-only for its owner", () => {
    const src = join(scratch, "src");
    put(src, "ro/file", "f", 0o444);
    chmodSync(join(src, "ro"), 0o555);
    try {
      copyTree(src, join(scratch, "dst"));
      assert.equal(octal(lstatSync(join(scratch, "dst", "ro")).mode), "755", "the owner keeps write access");
      assert.equal(readFileSync(join(scratch, "dst", "ro", "file"), "utf8"), "f");
    } finally {
      chmodSync(join(src, "ro"), 0o755);
      chmodSync(join(scratch, "dst", "ro"), 0o755);
    }
  });

  it("refuses a special file by name instead of skipping it", () => {
    const src = join(scratch, "src");
    put(src, "ok");
    assert.equal(spawnSync("mkfifo", [join(src, "pipe")]).status, 0);
    assert.throws(
      () => copyTree(src, join(scratch, "dst")),
      (err) => err instanceof PackageError && /pipe: .*FIFO/.test(err.message) && !err.message.includes("\n"),
    );
  });
});

describe("the audit of an output", () => {
  const output = () => join(scratch, "out");
  const problems = () => {
    try {
      auditOutput(output());
      return null;
    } catch (err) {
      assert.ok(err instanceof PackageError);
      return err.message;
    }
  };
  beforeEach(() => {
    put(output(), "bin/tool", "x", 0o755);
    put(output(), "data/file", "y");
    symlinkSync("../data/file", join(output(), "bin", "link"));
  });

  it("passes a tree whose links stay inside it and nothing is writable by others", () => {
    assert.equal(problems(), null);
  });

  it("names a file or directory that group or others can write", () => {
    chmodSync(join(output(), "data", "file"), 0o664);
    assert.match(problems(), /writable by group or others: data\/file/);
    chmodSync(join(output(), "data", "file"), 0o644);
    chmodSync(join(output(), "data"), 0o757);
    assert.match(problems(), /writable by group or others: data/);
  });

  it("names a symlink that is absolute, leaves the output, or points at nothing", () => {
    symlinkSync("/etc/hostname", join(output(), "abs"));
    assert.match(problems(), /absolute symlink: abs -> \/etc\/hostname/);
    rmSync(join(output(), "abs"));
    symlinkSync("../../elsewhere", join(output(), "bin", "out"));
    assert.match(problems(), /symlink leaves the output: bin\/out -> \.\.\/\.\.\/elsewhere/);
    rmSync(join(output(), "bin", "out"));
    symlinkSync("nothing", join(output(), "dangling"));
    assert.match(problems(), /dangling symlink: dangling -> nothing/);
  });

  it("names a file that is not a regular file, a directory or a link", () => {
    assert.equal(spawnSync("mkfifo", [join(output(), "pipe")]).status, 0);
    assert.match(problems(), /not a regular file, directory or symlink: pipe/);
  });
});

describe("replacing the output", () => {
  const final = () => join(scratch, "dist", OUTPUT);
  const leftovers = () => readdirSync(join(scratch, "dist")).filter((name) => name !== OUTPUT);
  const lay = (name) => async (work) => put(work, name, `${name}\n`);

  it("makes the output and leaves nothing beside it", async () => {
    await replaceOutput(final(), lay("one"));
    assert.deepEqual(
      snapshot(final()).map((line) => line.split(" ")[0]),
      ["one"],
    );
    assert.deepEqual(leftovers(), []);
  });

  it("replaces an earlier output as a whole", async () => {
    await replaceOutput(final(), lay("old"));
    await replaceOutput(final(), lay("new"));
    assert.deepEqual(readdirSync(final()), ["new"]);
    assert.deepEqual(leftovers(), []);
  });

  it("leaves the earlier output untouched and no folder behind when the build fails", async () => {
    await replaceOutput(final(), lay("old"));
    const before = snapshot(final());
    await assert.rejects(
      replaceOutput(final(), async (work) => {
        put(work, "half-built");
        throw new PackageError("the build broke");
      }),
      /the build broke/,
    );
    assert.deepEqual(snapshot(final()), before);
    assert.deepEqual(leftovers(), []);
  });

  it("puts the earlier output back when the new one cannot be moved into place", async () => {
    await replaceOutput(final(), lay("old"));
    const before = snapshot(final());
    await assert.rejects(
      replaceOutput(final(), async (work) => rmSync(work, { recursive: true })),
      /ENOENT/,
    );
    assert.deepEqual(snapshot(final()), before);
    assert.deepEqual(leftovers(), []);
  });

  it("works when there is no dist/ yet, and builds in a folder of its own beside the output", async () => {
    let seen;
    await replaceOutput(final(), async (work) => {
      seen = work;
      put(work, "x");
    });
    assert.equal(dirname(seen), dirname(final()));
    assert.match(basename(seen), new RegExp(`^\\.${OUTPUT}-${process.pid}$`));
    assert.equal(existsSync(seen), false);
  });

  it("removes what a killed run left, and keeps what a running one is using", async () => {
    mkdirSync(join(scratch, "dist"), { recursive: true });
    const gone = spawnSync(process.execPath, ["-e", ""]).pid; // a process id that is free again
    mkdirSync(join(scratch, "dist", `.${OUTPUT}-${gone}`));
    mkdirSync(join(scratch, "dist", `.old-${OUTPUT}-${gone}`));
    mkdirSync(join(scratch, "dist", `.${OUTPUT}-${process.ppid}`)); // the test runner is alive
    mkdirSync(join(scratch, "dist", `.${OUTPUT}-${process.pid}`)); // ours from an earlier run, the pid reused
    put(join(scratch, "dist", `.${OUTPUT}-${process.pid}`), "stale");
    // Names that only look like leftovers belong to someone else, whatever follows the prefix.
    const theirs = [`.${OUTPUT}-notes`, `.old-${OUTPUT}-`, `.${OUTPUT}-12abc`, `.${OUTPUT}`, `.keep-${OUTPUT}-${gone}`];
    for (const name of theirs) put(join(scratch, "dist", name), "mine");
    mkdirSync(join(scratch, "dist", "unrelated"));
    await replaceOutput(final(), lay("x"));
    assert.deepEqual(leftovers().sort(), [`.${OUTPUT}-${process.ppid}`, ...theirs, "unrelated"].sort());
  });
});

describe("an interrupt during a run", () => {
  for (const [signal, status] of [
    ["SIGINT", 130],
    ["SIGTERM", 143],
  ]) {
    it(`${signal} removes the folder being built and ends the run with status ${status}`, async () => {
      const dir = join(scratch, "being-built");
      const code = `
        import { mkdirSync } from "node:fs";
        import { onInterrupt, exitOnInterrupt } from ${JSON.stringify(pathToFileURL(realScript).href)};
        import { rmSync } from "node:fs";
        mkdirSync(${JSON.stringify(dir)});
        onInterrupt(() => rmSync(${JSON.stringify(dir)}, { recursive: true, force: true }));
        exitOnInterrupt();
        console.log("ready");
        setInterval(() => {}, 1000);
      `;
      const child = spawn(process.execPath, ["--input-type=module", "-e", code], {
        stdio: ["ignore", "pipe", "inherit"],
      });
      await new Promise((resolve) => child.stdout.once("data", resolve));
      assert.equal(existsSync(dir), true);
      const ended = new Promise((resolve) => child.once("exit", (exitCode, sig) => resolve({ exitCode, sig })));
      child.kill(signal);
      assert.deepEqual(await ended, { exitCode: status, sig: null });
      assert.equal(existsSync(dir), false);
    });
  }
});

describe("a signal that arrives during a step", () => {
  for (const [signal, status] of [
    ["SIGINT", 130],
    ["SIGTERM", 143],
  ]) {
    it(`${signal} stops the run before the earlier output is replaced`, () => {
      const final = join(scratch, "dist", OUTPUT);
      put(final, "old");
      // The build sends the signal to its own process in the middle of its step. The steps are synchronous, so the
      // handler can only run when the run lets the event loop turn; it has to do so before it swaps.
      const code = `
        import { writeFileSync } from "node:fs";
        import { join } from "node:path";
        import { exitOnInterrupt, replaceOutput } from ${JSON.stringify(pathToFileURL(realScript).href)};
        exitOnInterrupt();
        await replaceOutput(${JSON.stringify(final)}, async (work) => {
          writeFileSync(join(work, "new"), "new");
          process.kill(process.pid, ${JSON.stringify(signal)});
        });
        console.log("finished");
      `;
      const run = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
      assert.equal(run.status, status, run.stderr);
      assert.equal(run.stdout, "", "the run did not go on to the end");
      assert.deepEqual(readdirSync(final), ["old"], "the earlier output is the one that stays");
      assert.deepEqual(readdirSync(join(scratch, "dist")), [OUTPUT], "no folder is left beside it");
    });
  }
});

describe("the script on a fake tree", () => {
  let desktop;
  beforeEach(() => {
    desktop = fakeDesktop();
  });
  const dist = () => join(desktop, "node_modules", "electron", "dist");
  const payload = () => join(desktop, "payload", "linux-x64");
  const fails = (run, status, pattern) => {
    assert.equal(run.status, status, run.stderr);
    assert.equal(run.stdout, "");
    const lines = run.stderr.trim().split("\n");
    assert.equal(lines.length, 1, `one line on stderr, got: ${run.stderr}`);
    assert.match(lines[0], pattern);
    assert.doesNotMatch(run.stderr, /\n\s+at /);
  };

  it("lays out the unpacked app: Electron renamed, its default app gone, src, backend and package.json added", () => {
    const run = runIn(desktop);
    assert.equal(run.status, 0, run.stderr);
    assert.equal(run.stderr, "");
    const out = outputOf(desktop);

    // the Electron distribution, byte for byte and mode for mode, except the rename and the removed default app
    const wanted = new Set();
    for (const line of snapshot(dist())) {
      if (line.startsWith("resources/default_app.asar ")) continue;
      wanted.add(line.startsWith("electron ") ? line.replace(/^electron /, `${BINARY} `) : line);
    }
    // what is added
    wanted.add("resources/app/ 755");
    wanted.add("resources/app/src/ 755");
    for (const line of snapshot(join(desktop, "src"))) wanted.add(`resources/app/src/${line}`);
    wanted.add("resources/backend/ 755");
    for (const line of snapshot(payload())) wanted.add(`resources/backend/${line}`);
    const packageLine = snapshot(out).find((line) => line.startsWith("resources/app/package.json "));
    assert.ok(packageLine, "resources/app/package.json exists");
    wanted.add(packageLine);

    const got = snapshot(out);
    assert.deepEqual(
      got.filter((line) => !wanted.has(line)),
      [],
      "nothing else is in the output",
    );
    assert.deepEqual(
      [...wanted].filter((line) => !got.includes(line)),
      [],
      "everything wanted is in the output",
    );
    assert.equal(existsSync(join(out, "electron")), false);
    assert.equal(existsSync(join(out, "resources", "default_app.asar")), false);
    assert.equal(lstatSync(join(out, "libfoo.so")).isSymbolicLink(), true, "a link in the distribution stays a link");
    assert.equal(readlinkSync(join(out, "libfoo.so")), "libfoo.so.1");
    assert.equal(readlinkSync(join(out, "resources", "backend", "bin", "python3")), "python3.12");
    assert.equal(octal(lstatSync(join(out, BINARY)).mode), "755");
    assert.equal(octal(lstatSync(join(out, "resources", "backend", "bin", "python3.12")).mode), "755");
  });

  it("copies nothing of desktop/ but src/ (no tests, lockfile or .npmrc) and no node_modules", () => {
    assert.equal(runIn(desktop).status, 0);
    const files = snapshot(outputOf(desktop)).map((line) => line.split(" ")[0]);
    for (const name of ["package-lock.json", ".npmrc", "x.test.mjs", "build-backend.mjs", "package-linux.mjs"]) {
      assert.equal(
        files.some((path) => path.endsWith(name)),
        false,
        name,
      );
    }
    assert.equal(
      files.some((path) => path.includes("node_modules") || path.includes("/test/")),
      false,
    );
  });

  it("writes a package.json with only the named fields and the commit, unknown without git", () => {
    assert.equal(runIn(desktop).status, 0);
    const text = readFileSync(join(outputOf(desktop), "resources", "app", "package.json"), "utf8");
    assert.deepEqual(JSON.parse(text), {
      name: "aihub-desktop",
      productName: "aihub",
      version: "0.1.0",
      desktopName: "dev.akhil-p.aihub.desktop",
      main: "src/main.js",
      type: "module",
      aihubCommit: "unknown",
    });
    assert.ok(text.endsWith("}\n"));
  });

  it("puts the commit of a git checkout into package.json, with -dirty when desktop/ has changes", () => {
    const root = dirname(desktop);
    const full = gitInit(root);
    assert.equal(runIn(desktop).status, 0);
    const read = () =>
      JSON.parse(readFileSync(join(outputOf(desktop), "resources", "app", "package.json"), "utf8")).aihubCommit;
    assert.equal(read(), full.slice(0, 12));
    put(desktop, "src/main.js", "edited\n");
    assert.equal(runIn(desktop).status, 0);
    assert.equal(read(), `${full.slice(0, 12)}-dirty`);
  });

  it("prints the output path, its size and file count, the commit and the Electron version", () => {
    const run = runIn(desktop);
    assert.equal(run.status, 0);
    assert.match(run.stdout, /^output +desktop\/dist\/linux-unpacked$/m);
    assert.match(run.stdout, /^size +\d+\.\d MiB, \d+ files, 2 symlinks$/m); // libfoo.so and the payload's bin/python3
    assert.match(run.stdout, /^commit +unknown$/m);
    assert.match(run.stdout, /^electron +44\.6\.0$/m);
  });

  it("makes nothing writable by group or others, whatever the umask or the modes of the inputs", () => {
    chmodSync(join(dist(), "locales"), 0o777);
    chmodSync(join(dist(), "resources.pak"), 0o666);
    chmodSync(join(desktop, "src", "main.js"), 0o664);
    chmodSync(join(payload(), "bin", "pip-stand-in"), 0o775);
    const run = runIn(desktop, [], { prefix: ["sh", "-c", 'umask 000; exec "$@"', "sh"] });
    assert.equal(run.status, 0, run.stderr);
    const bad = [];
    const visit = (rel) => {
      for (const entry of readdirSync(join(outputOf(desktop), rel), { withFileTypes: true })) {
        const path = rel ? `${rel}/${entry.name}` : entry.name;
        if (!entry.isSymbolicLink() && lstatSync(join(outputOf(desktop), path)).mode & 0o022) bad.push(path);
        if (entry.isDirectory()) visit(path);
      }
    };
    visit("");
    assert.deepEqual(bad, []);
    assert.equal(octal(lstatSync(join(outputOf(desktop), "resources", "backend", "bin", "pip-stand-in")).mode), "755");
  });

  it("gives the same output when run twice, replaces the first, and leaves only linux-unpacked in dist/", () => {
    assert.equal(runIn(desktop).status, 0);
    const first = snapshot(outputOf(desktop));
    put(outputOf(desktop), "stale-file-of-an-older-run");
    assert.equal(runIn(desktop).status, 0);
    assert.deepEqual(snapshot(outputOf(desktop)), first);
    assert.deepEqual(readdirSync(join(desktop, "dist")), [OUTPUT]);
  });

  it("works with no network at all", () => {
    const probe = spawnSync("unshare", ["-rn", "true"]);
    assert.equal(probe.status, 0, "unshare -rn is needed for this test");
    const run = runIn(desktop, [], { prefix: ["unshare", "-rn"] });
    assert.equal(run.status, 0, run.stderr);
    assert.equal(existsSync(join(outputOf(desktop), BINARY)), true);
  });

  it("refuses an argument with status 2 and builds nothing", () => {
    fails(runIn(desktop, ["linux"]), 2, /^package-linux: usage: node scripts\/package-linux\.mjs/);
    assert.equal(existsSync(join(desktop, "dist")), false);
  });

  it("refuses a missing Electron distribution or payload in one line, and builds nothing", () => {
    rmSync(join(desktop, "node_modules", "electron", "dist", "electron"));
    fails(
      runIn(desktop),
      1,
      /^package-linux: node_modules\/electron\/dist\/electron is missing: run node node_modules\/electron\/install\.js in desktop\//,
    );
    assert.equal(existsSync(join(desktop, "dist")), false);

    desktop = fakeDesktop(join(scratch, "second"));
    rmSync(join(payload(), "bin", "python3.12"));
    fails(
      runIn(desktop),
      1,
      /^package-linux: payload\/linux-x64 is incomplete \(missing bin\/python3\.12\): run npm run build:backend/,
    );
    assert.equal(existsSync(join(desktop, "dist")), false);
  });

  it("leaves an earlier output exactly as it was when a step fails, and no folder behind", () => {
    assert.equal(runIn(desktop).status, 0);
    const before = snapshot(outputOf(desktop));
    const attempts = [
      [
        "an unreadable file in src/",
        () => chmodSync(join(desktop, "src", "deep", "er", "module.js"), 0o000),
        /module\.js/,
        () => chmodSync(join(desktop, "src", "deep", "er", "module.js"), 0o644),
      ],
      [
        "an unreadable file in the Electron distribution",
        () => chmodSync(join(dist(), "resources.pak"), 0o000),
        /resources\.pak/,
        () => chmodSync(join(dist(), "resources.pak"), 0o644),
      ],
      [
        "an unreadable file in the payload",
        () => chmodSync(join(payload(), "lib", "python3.12", "os.py"), 0o000),
        /os\.py/,
        () => chmodSync(join(payload(), "lib", "python3.12", "os.py"), 0o644),
      ],
      [
        "a symlink in src/ that points outside",
        () => symlinkSync("../../../../outside", join(desktop, "src", "escape")),
        /symlink leaves the output: resources\/app\/src\/escape/,
        () => rmSync(join(desktop, "src", "escape")),
      ],
      [
        "a special file in src/",
        () => spawnSync("mkfifo", [join(desktop, "src", "pipe")]),
        /pipe: .*FIFO/,
        () => rmSync(join(desktop, "src", "pipe")),
      ],
    ];
    for (const [what, break_, pattern, restore] of attempts) {
      break_();
      const run = runIn(desktop);
      fails(run, 1, pattern);
      assert.deepEqual(snapshot(outputOf(desktop)), before, `${what}: the earlier output is untouched`);
      assert.deepEqual(readdirSync(join(desktop, "dist")), [OUTPUT], `${what}: nothing is left beside it`);
      restore();
    }
    assert.equal(runIn(desktop).status, 0, "and it works again once the cause is gone");
  });

  it("refuses a payload whose aihub_web is not the code in app/", () => {
    const root = dirname(desktop);
    gitInit(root);
    assert.equal(runIn(desktop).status, 0);
    put(root, "app/aihub_web/__main__.py", "changed\n");
    fails(
      runIn(desktop),
      1,
      /^package-linux: payload\/linux-x64 holds a different aihub_web than app\/ .*build:backend/,
    );
  });

  it("calls a change to uv.lock dirty, and does not package a git-ignored file unnoticed", () => {
    const root = dirname(desktop);
    const full = gitInit(root);
    put(root, "uv.lock", "a\n");
    git(root, "add", ".");
    git(root, "commit", "-q", "-m", "lock");
    put(root, "uv.lock", "b\n");
    assert.match(commitString(root), /-dirty$/);
    assert.ok(full);
  });

  it("is not fooled by a link that leaves through another link", () => {
    symlinkSync("..", join(dist(), "resources", "up"));
    symlinkSync("up/../..", join(dist(), "resources", "chain"));
    fails(runIn(desktop), 1, /symlink leaves the output: resources\/chain/);
  });

  it("refuses an empty src and puts nothing in place", () => {
    assert.equal(runIn(desktop).status, 0);
    const before = snapshot(outputOf(desktop));
    rmSync(join(desktop, "src"), { recursive: true });
    mkdirSync(join(desktop, "src"));
    fails(runIn(desktop), 1, /resources\/app\/src\/main\.js is missing/);
    assert.deepEqual(snapshot(outputOf(desktop)), before);
  });

  it("copes with a read-only directory in an input on every run", () => {
    chmodSync(join(desktop, "src", "deep"), 0o555);
    for (let i = 0; i < 3; i++) assert.equal(runIn(desktop).status, 0);
    chmodSync(join(desktop, "src", "deep"), 0o755);
    assert.deepEqual(readdirSync(join(desktop, "dist")), [OUTPUT]);
  });

  it("puts back the earlier output that a run killed between its renames left", () => {
    assert.equal(runIn(desktop).status, 0);
    const before = snapshot(outputOf(desktop));
    const gone = spawnSync(process.execPath, ["-e", ""]).pid;
    renameSync(outputOf(desktop), join(desktop, "dist", `.old-${OUTPUT}-${gone}`));
    chmodSync(join(desktop, "src", "main.js"), 0o000); // the run fails while copying, after the restore
    fails(runIn(desktop), 1, /main\.js/);
    chmodSync(join(desktop, "src", "main.js"), 0o644);
    assert.deepEqual(snapshot(outputOf(desktop)), before);
  });

  it("does not use the network, a shell or anything but git as a child process", () => {
    const source = readFileSync(realScript, "utf8");
    assert.doesNotMatch(source, /node:(https?|http2|net|tls|dns|dgram)\b|\bfetch\(/);
    // Child processes: one import, of execFileSync alone (no exec, execSync or spawn, which take a shell string or
    // can take a shell option), used for git alone.
    assert.equal(source.match(/node:child_process/g).length, 1);
    assert.deepEqual(
      [...source.matchAll(/import\s*\{([^}]*)\}\s*from\s*"node:child_process"/g)].map((m) => m[1].trim()),
      ["execFileSync"],
    );
    assert.doesNotMatch(source, /shell:\s*true/);
    assert.deepEqual(
      [...source.matchAll(/execFileSync\(\s*"([^"]+)"/g)].map((m) => m[1]),
      ["git"],
    );
  });
});
