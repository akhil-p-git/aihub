// scripts/build-backend.mjs without the network, the real uv or the real archive: its pure parts, and the parts that
// run uv and tar, against a stand-in uv that records how it was called and the real tar on a tiny archive.

import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  statSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { delimiter, dirname, join, resolve } from "node:path";
import { afterEach, beforeEach, describe, it, mock } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

import {
  BuildError,
  DOWNLOAD_TIMEOUT_MS,
  PBS_TAG,
  PYTHON_VERSION,
  TARGETS,
  ageWarning,
  archiveName,
  archiveUrl,
  assertHttps,
  auditPayload,
  buildAtomically,
  canRunTarget,
  downloadFile,
  exportRequirements,
  extract,
  failureReason,
  fetchVerified,
  installDependencies,
  parseTarget,
  pbsAgeDays,
  prune,
  requiredFiles,
  sha256File,
  shouldPrune,
  tarExtractArgs,
  uvExportArgs,
  uvInstallArgs,
  verifyArchive,
} from "../scripts/build-backend.mjs";

const script = join(dirname(fileURLToPath(import.meta.url)), "..", "scripts", "build-backend.mjs");

let scratch;
beforeEach(() => {
  scratch = mkdtempSync(join(tmpdir(), "build-backend-test-"));
});
afterEach(() => {
  rmSync(scratch, { recursive: true, force: true });
});

/** Create the given files (a path ending in "/" is an empty directory, "a -> b" a symlink). */
function lay(root, entries) {
  for (const entry of entries) {
    const [path, target] = entry.split(" -> ");
    const full = join(root, path);
    if (path.endsWith("/")) {
      mkdirSync(full, { recursive: true });
    } else {
      mkdirSync(dirname(full), { recursive: true });
      if (target) symlinkSync(target, full);
      else writeFileSync(full, `contents of ${path}\n`);
    }
  }
}

/** Every entry below root, as sorted "/"-separated relative paths. */
function listing(root, rel = "") {
  const names = readdirSync(join(root, rel), { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : 1));
  return names.flatMap((entry) => {
    const path = rel ? `${rel}/${entry.name}` : entry.name;
    return entry.isDirectory() ? [path, ...listing(root, path)] : [path];
  });
}

/**
 * Stand-ins for a binary: the magic number of an ELF or a 64-bit Mach-O file, then the library names that the
 * loader would read from it, each as a NUL-terminated string (the form they have in .dynstr and in a dylib load
 * command).
 */
const withNames = (magic, names) =>
  Buffer.concat([Buffer.from(magic), ...names.map((name) => Buffer.from(`\0${name}\0`))]);
const elf = (...names) => withNames([0x7f, 0x45, 0x4c, 0x46], names);
const macho = (...names) => withNames([0xcf, 0xfa, 0xed, 0xfe], names);

describe("the pin table", () => {
  it("has linux-x64, mac-arm64 and mac-x64, each with a known triple and a 64-hex sha256", () => {
    assert.deepEqual(Object.keys(TARGETS).sort(), ["linux-x64", "mac-arm64", "mac-x64"]);
    assert.deepEqual(Object.fromEntries(Object.entries(TARGETS).map(([name, t]) => [name, t.triple])), {
      "linux-x64": "x86_64-unknown-linux-gnu",
      "mac-arm64": "aarch64-apple-darwin",
      "mac-x64": "x86_64-apple-darwin",
    });
    for (const [name, target] of Object.entries(TARGETS)) assert.match(target.sha256, /^[0-9a-f]{64}$/, name);
    assert.equal(new Set(Object.values(TARGETS).map((t) => t.sha256)).size, 3, "each archive has its own hash");
  });

  // A new release changes the tag, the version and the three hashes together, here and in the script.
  it("pins CPython 3.12.15 from python-build-standalone release 20261003", () => {
    assert.equal(PYTHON_VERSION, "3.12.15");
    assert.equal(PBS_TAG, "20261003");
  });

  it("holds the sha256 values that the release's SHA256SUMS file lists for the three archives", () => {
    // https://github.com/astral-sh/python-build-standalone/releases/download/20261003/SHA256SUMS
    assert.deepEqual(Object.fromEntries(Object.entries(TARGETS).map(([name, { sha256 }]) => [name, sha256])), {
      "linux-x64": "731af898886c5f821890dc901eca3c651cca8e51fa7308c159d12a1194aeac91",
      "mac-arm64": "ad8d0c637c0a36b967b310e2c07254f4d2ca8cabaa7699e55ed6290aceb481a2",
      "mac-x64": "562c30864ece2cb1d3e0ad66a1acd498611a47e5a10ce81b99158bef1ccbd355",
    });
  });

  it("names the install_only_stripped archive of the pinned release for each target", () => {
    assert.deepEqual(Object.keys(TARGETS).map(archiveName), [
      "cpython-3.12.15+20261003-x86_64-unknown-linux-gnu-install_only_stripped.tar.gz",
      "cpython-3.12.15+20261003-aarch64-apple-darwin-install_only_stripped.tar.gz",
      "cpython-3.12.15+20261003-x86_64-apple-darwin-install_only_stripped.tar.gz",
    ]);
  });

  it("downloads over https from the release's asset path, with the + written as %2B", () => {
    const url = new URL(archiveUrl("mac-arm64"));
    assert.equal(url.protocol, "https:");
    assert.equal(url.host, "github.com");
    assert.equal(
      url.pathname,
      "/astral-sh/python-build-standalone/releases/download/20261003/" +
        "cpython-3.12.15%2B20261003-aarch64-apple-darwin-install_only_stripped.tar.gz",
    );
  });

  it("refuses a URL that is not https", () => {
    for (const bad of ["http://github.com/x", "ftp://github.com/x", "file:///etc/passwd", "not a url"]) {
      assert.throws(() => assertHttps(bad), BuildError, bad);
    }
    assert.doesNotThrow(() => assertHttps("https://github.com/x"));
  });
});

describe("verifyArchive", () => {
  const bytes = Buffer.from("a stand-in for an archive ".repeat(4000));
  const sha256 = createHash("sha256").update(bytes).digest("hex");

  it("hashes like sha256sum (the NIST test vector for abc)", async () => {
    const file = join(scratch, "abc");
    writeFileSync(file, "abc");
    assert.equal(await sha256File(file), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
  });

  it("accepts the matching bytes and keeps the file", async () => {
    const file = join(scratch, "archive.tar.gz");
    writeFileSync(file, bytes);
    await verifyArchive(file, sha256);
    assert.ok(existsSync(file));
  });

  it("rejects a one-byte change, names both digests and deletes the file", async () => {
    const changed = Buffer.from(bytes);
    changed[changed.length >> 1] ^= 1;
    const file = join(scratch, "archive.tar.gz");
    writeFileSync(file, changed);
    await assert.rejects(
      verifyArchive(file, sha256),
      (err) =>
        err instanceof BuildError &&
        /sha256 mismatch/.test(err.message) &&
        err.message.includes(sha256) &&
        err.message.includes(createHash("sha256").update(changed).digest("hex")),
    );
    assert.ok(!existsSync(file), "a file that fails the check is deleted");
  });

  it("rejects a truncated file and an empty one, and deletes them", async () => {
    for (const cut of [bytes.length - 1, 0]) {
      const file = join(scratch, `cut-${cut}`);
      writeFileSync(file, bytes.subarray(0, cut));
      await assert.rejects(verifyArchive(file, sha256), /sha256 mismatch/, `cut at ${cut}`);
      assert.ok(!existsSync(file));
    }
  });

  it("compares all 64 characters: a pin that differs in any one of them is a mismatch", async () => {
    const file = join(scratch, "archive.tar.gz");
    for (let at = 0; at < sha256.length; at++) {
      const other = sha256[at] === "0" ? "1" : "0";
      const wrong = sha256.slice(0, at) + other + sha256.slice(at + 1);
      writeFileSync(file, bytes);
      await assert.rejects(verifyArchive(file, wrong), /sha256 mismatch/, `character ${at}`);
      assert.ok(!existsSync(file), `character ${at}: the file is deleted`);
    }
  });

  it("compares the whole lowercase digest: a shorter, longer or upper-case pin is a mismatch", async () => {
    const file = join(scratch, "archive.tar.gz");
    for (const wrong of [sha256.slice(0, 63), sha256.slice(0, 32), `${sha256}0`, sha256.toUpperCase(), ""]) {
      writeFileSync(file, bytes);
      await assert.rejects(verifyArchive(file, wrong), /sha256 mismatch/, JSON.stringify(wrong));
    }
  });
});

describe("parseTarget", () => {
  it("accepts each known target", () => {
    for (const name of Object.keys(TARGETS)) assert.equal(parseTarget([name]), name);
  });

  it("refuses a missing, unknown or extra argument with status 2 and says what is accepted", () => {
    for (const args of [[], ["windows-x64"], ["linux-x64", "mac-x64"], ["--help"]]) {
      assert.throws(
        () => parseTarget(args),
        (err) => err instanceof BuildError && err.status === 2 && /linux-x64.*mac-arm64.*mac-x64/.test(err.message),
        JSON.stringify(args),
      );
    }
  });
});

describe("running the script", () => {
  it("exits 2 with a one-line message for an unknown target", () => {
    const run = spawnSync(process.execPath, [script, "windows-x64"], { encoding: "utf8" });
    assert.equal(run.status, 2, run.stderr);
    assert.equal(run.stdout, "");
    assert.match(run.stderr, /unknown target "windows-x64"/);
    assert.equal(run.stderr.trim().split("\n").length, 1);
  });

  it("exits 2 when no target is given", () => {
    const run = spawnSync(process.execPath, [script], { encoding: "utf8" });
    assert.equal(run.status, 2, run.stderr);
    assert.match(run.stderr, /usage: node scripts\/build-backend\.mjs/);
  });

  it("can be imported without building anything", () => {
    const code = `const m = await import(${JSON.stringify(pathToFileURL(script).href)}); console.log(typeof m.main);`;
    const run = spawnSync(process.execPath, ["--input-type=module", "-e", code], { encoding: "utf8" });
    assert.equal(run.status, 0, run.stderr);
    assert.equal(run.stdout.trim(), "function");
  });
});

describe("shouldPrune", () => {
  const removed = [
    // launchers: only the interpreter stays in bin/
    "bin/python3",
    "bin/python",
    "bin/pip3.12",
    "bin/idle3.12",
    "bin/2to3-3.12",
    "bin/pydoc3",
    "bin/python3-config",
    "bin/python3.12-config",
    // development and documentation files
    "include",
    "include/python3.12/Python.h",
    "share/man/man1/python3.1",
    "share/terminfo/x/xterm",
    "lib/pkgconfig/python3.pc",
    "lib/python3.12/config-3.12-darwin",
    "lib/python3.12/config-3.12-x86_64-linux-gnu/Makefile",
    // the shared libpython: a second copy of the interpreter, which bin/python3.12 does not link
    "lib/libpython3.12.so.1.0",
    "lib/libpython3.12.so",
    "lib/libpython3.so",
    "lib/libpython3.12.dylib",
    "lib/libpython3.12.a",
    // stdlib packages the app never imports
    "lib/python3.12/ensurepip",
    "lib/python3.12/idlelib/idle.py",
    "lib/python3.12/lib2to3/main.py",
    "lib/python3.12/pydoc_data/topics.py",
    "lib/python3.12/turtledemo/bytedesign.py",
    "lib/python3.12/tkinter/__init__.py",
    // extension modules with system library or Tcl/Tk dependencies
    "lib/python3.12/lib-dynload/_dbm.cpython-312-darwin.so",
    "lib/python3.12/lib-dynload/_crypt.cpython-312-x86_64-linux-gnu.so",
    "lib/python3.12/lib-dynload/_tkinter.cpython-312-darwin.so",
    // Tcl/Tk itself
    "lib/tcl9.0",
    "lib/tcl9/9.0/http-2.10.0.tm",
    "lib/tk9.0/tk.tcl",
    "lib/itcl4.3.8/itcl.tcl",
    "lib/thread3.0.6/pkgIndex.tcl",
    "lib/libtcl9.0.so",
    "lib/libtcl9tk9.0.dylib",
    // pip and what uv --target leaves behind
    "lib/python3.12/site-packages/pip",
    "lib/python3.12/site-packages/pip/__init__.py",
    "lib/python3.12/site-packages/pip-26.2.1.dist-info",
    "lib/python3.12/site-packages/bin",
    "lib/python3.12/site-packages/bin/uvicorn",
    "lib/python3.12/site-packages/.lock",
    // bytecode, wherever it is
    "lib/python3.12/encodings/__pycache__",
    "lib/python3.12/encodings/__pycache__/utf_8.cpython-312.pyc",
    "lib/python3.12/site-packages/pydantic/__pycache__",
    "lib/python3.12/legacy.pyc",
  ];
  const kept = [
    "bin",
    "bin/python3.12",
    "lib",
    "lib/python3.12",
    "lib/python3.12/os.py",
    "lib/python3.12/encodings/utf_8.py",
    "lib/python3.12/sqlite3/__init__.py",
    "lib/python3.12/dbm/__init__.py",
    "lib/python3.12/crypt.py",
    "lib/python3.12/lib-dynload",
    "lib/python3.12/lib-dynload/.empty",
    "lib/python3.12/site-packages",
    "lib/python3.12/site-packages/README.txt",
    "lib/python3.12/site-packages/aihub_web/static/index.html",
    "lib/python3.12/site-packages/pydantic-2.13.5.dist-info/RECORD",
    // names that only start like something that goes
    "lib/python3.12/site-packages/pipeline/stage.py",
    "lib/python3.12/site-packages/binary/codec.py",
    "lib/python3.12/site-packages/lock.py",
    "lib/python3.12/site-packages/idna/core.py",
    "lib/python3.12/config.py",
    "lib/threading_helpers.py",
    "lib/python3.12/site-packages/thread2/x.py",
    "lib/libpythonic.so",
  ];
  // The shared libraries that can be loaded by the interpreter or a wheel: never matched.
  const sharedLibraries = [
    "lib/python3.12/lib-dynload/_ssl.cpython-312-x86_64-linux-gnu.so",
    "lib/python3.12/site-packages/pydantic_core/_pydantic_core.cpython-312-x86_64-linux-gnu.so",
    "lib/python3.12/site-packages/pydantic_core/_pydantic_core.cpython-312-darwin.so",
    "lib/python3.12/site-packages/yaml/_yaml.cpython-312-x86_64-linux-gnu.so",
    "lib/python3.12/site-packages/yaml/_yaml.cpython-312-darwin.so",
  ];

  it("matches everything the blueprint prunes", () => {
    for (const path of removed) assert.ok(shouldPrune(path), `${path} should go`);
  });

  it("keeps the interpreter, the stdlib and the packages", () => {
    for (const path of kept) assert.ok(!shouldPrune(path), `${path} should stay`);
  });

  it("never matches python3.12 or a shared library the interpreter or a wheel can load", () => {
    for (const path of ["bin/python3.12", ...sharedLibraries]) assert.ok(!shouldPrune(path), `${path} should stay`);
  });

  it("matches a shared library only when it is libpython, Tcl/Tk or the _dbm, _crypt or _tkinter module", () => {
    const shared = /\.(so(\.\d+)*|dylib)$/;
    const allowed = /lib-dynload\/(_dbm|_crypt|_tkinter)[^/]*$|^lib\/lib(tcl|tk|python)[^/]*$/;
    for (const path of removed.filter((p) => shared.test(p))) assert.match(path, allowed);
  });
});

describe("prune", () => {
  it("removes exactly what shouldPrune matches and leaves the kept files untouched", () => {
    const root = join(scratch, "payload");
    lay(root, [
      "bin/python3 -> python3.12",
      "bin/pip",
      "include/python3.12/Python.h",
      "lib/libpython3.12.so.1.0",
      "lib/libpython3.12.so -> libpython3.12.so.1.0",
      "lib/tcl9.0/init.tcl",
      "lib/python3.12/os.py",
      "lib/python3.12/__pycache__/os.cpython-312.pyc",
      "lib/python3.12/lib-dynload/.empty",
      "lib/python3.12/lib-dynload/_dbm.cpython-312-darwin.so",
      "lib/python3.12/site-packages/.lock",
      "lib/python3.12/site-packages/bin/uvicorn",
      "lib/python3.12/site-packages/pkg/mod.py",
      "lib/python3.12/site-packages/pkg/__pycache__/mod.cpython-312.pyc",
    ]);
    const interpreter = elf("libm.so.6", "libc.so.6");
    writeFileSync(join(root, "bin/python3.12"), interpreter);

    const { entries, bytes } = prune(root);

    assert.deepEqual(listing(root), [
      "bin",
      "bin/python3.12",
      "lib",
      "lib/python3.12",
      "lib/python3.12/lib-dynload",
      "lib/python3.12/lib-dynload/.empty",
      "lib/python3.12/os.py",
      "lib/python3.12/site-packages",
      "lib/python3.12/site-packages/pkg",
      "lib/python3.12/site-packages/pkg/mod.py",
    ]);
    assert.deepEqual(readFileSync(join(root, "bin/python3.12")), interpreter);
    // bin/python3, bin/pip, include, 2 x libpython, lib/tcl9.0, 2 x __pycache__, _dbm, .lock, site-packages/bin
    assert.equal(entries, 11);
    assert.ok(bytes > 0);
  });

  it("does not follow a symlink out of the payload", () => {
    const outside = join(scratch, "outside");
    lay(outside, ["__pycache__/keep.pyc"]);
    const root = join(scratch, "payload");
    lay(root, ["lib/link -> ../../outside"]);
    prune(root);
    assert.ok(existsSync(join(outside, "__pycache__", "keep.pyc")));
  });
});

describe("prune and the shared libraries it removes", () => {
  const root = () => join(scratch, "payload");
  const withLibpython = [
    "lib/libpython3.12.so.1.0",
    "lib/libpython3.12.so -> libpython3.12.so.1.0",
    "lib/libpython3.so",
  ];

  it("refuses, before it deletes anything, when an ELF file that stays links a library that would go", () => {
    lay(root(), [...withLibpython, "lib/python3.12/os.py"]);
    mkdirSync(join(root(), "bin"));
    writeFileSync(join(root(), "bin/python3.12"), elf("libm.so.6", "libpython3.12.so.1.0", "libc.so.6"));
    const before = listing(root());

    assert.throws(
      () => prune(root()),
      (err) =>
        err instanceof BuildError &&
        err.message.startsWith("bin/python3.12 links libpython3.12.so.1.0, which the prune would delete") &&
        !err.message.includes("\n"),
    );
    assert.deepEqual(listing(root()), before, "nothing was deleted");
  });

  it("does the same for a Mach-O file that names the dylib by @rpath, and for an extension module", () => {
    lay(root(), ["lib/libpython3.12.dylib", "lib/python3.12/os.py"]);
    mkdirSync(join(root(), "lib/python3.12/site-packages/ext"), { recursive: true });
    const module = join(root(), "lib/python3.12/site-packages/ext/_ext.cpython-312-darwin.so");
    writeFileSync(module, macho("/usr/lib/libSystem.B.dylib", "@rpath/libpython3.12.dylib"));
    assert.throws(
      () => prune(root()),
      (err) =>
        err instanceof BuildError &&
        err.message.startsWith("lib/python3.12/site-packages/ext/_ext.cpython-312-darwin.so links libpython3.12.dylib"),
    );
    assert.ok(existsSync(join(root(), "lib/libpython3.12.dylib")));
  });

  it("lets files that stay mention a name without linking it", () => {
    lay(root(), [...withLibpython, "lib/python3.12/os.py"]);
    mkdirSync(join(root(), "bin"));
    // a source file that talks about the library (sysconfig data does)
    writeFileSync(join(root(), "lib/python3.12/_sysconfigdata.py"), "LDLIBRARY = 'libpython3.12.so.1.0'\0\n");
    // a binary with a longer name that only starts the same, and one that links only what stays
    writeFileSync(join(root(), "bin/python3.12"), elf("libpython3.12.so.1.0.1", "libm.so.6"));
    prune(root());
    assert.deepEqual(listing(root()), [
      "bin",
      "bin/python3.12",
      "lib",
      "lib/python3.12",
      "lib/python3.12/_sysconfigdata.py",
      "lib/python3.12/os.py",
    ]);
  });

  it("checks only the binaries that stay: a library that links another one that goes is removed with it", () => {
    lay(root(), ["bin/python3.12", "lib/python3.12/os.py"]);
    writeFileSync(join(root(), "lib/libpython3.12.so.1.0"), elf("libc.so.6"));
    writeFileSync(join(root(), "lib/libpython3.so"), elf("libpython3.12.so.1.0"));
    prune(root());
    assert.deepEqual(listing(root()), ["bin", "bin/python3.12", "lib", "lib/python3.12", "lib/python3.12/os.py"]);
  });
});

describe("requiredFiles", () => {
  const site = "lib/python3.12/site-packages";
  const common = [
    "bin/python3.12",
    "lib/python3.12/os.py",
    `${site}/aihub_web/__main__.py`,
    `${site}/aihub_web/static/index.html`,
  ];

  it("lists the interpreter, the app and the two compiled dependencies of each target, and no libpython", () => {
    assert.deepEqual(requiredFiles("linux-x64"), [
      ...common,
      `${site}/pydantic_core/_pydantic_core.cpython-312-x86_64-linux-gnu.so`,
      `${site}/yaml/_yaml.cpython-312-x86_64-linux-gnu.so`,
    ]);
    for (const target of ["mac-arm64", "mac-x64"]) {
      assert.deepEqual(requiredFiles(target), [
        ...common,
        `${site}/pydantic_core/_pydantic_core.cpython-312-darwin.so`,
        `${site}/yaml/_yaml.cpython-312-darwin.so`,
      ]);
    }
  });
});

describe("auditPayload", () => {
  const required = ["bin/python3.12", "lib/python3.12/site-packages/pkg/mod.py"];
  const good = [
    "bin/python3.12",
    "lib/python3.12/os.py",
    "lib/python3.12/site-packages/pkg/mod.py",
    "lib/python3.12/site-packages/pkg/data -> mod.py",
  ];

  let audits = 0;
  function messageFor(entries, mutate = () => {}) {
    const root = join(scratch, `payload-${++audits}`);
    lay(root, entries);
    mutate(root);
    try {
      auditPayload(root, required);
    } catch (err) {
      assert.ok(err instanceof BuildError, String(err));
      return err.message;
    }
    return null;
  }

  it("passes a clean payload", () => {
    assert.equal(messageFor(good), null);
  });

  it("names a required file that is missing", () => {
    assert.match(messageFor(good.filter((e) => !e.startsWith("bin/"))), /missing bin\/python3\.12/);
  });

  it("names a world-writable file", () => {
    const message = messageFor(good, (root) => chmodSync(join(root, "lib/python3.12/site-packages/pkg/mod.py"), 0o666));
    assert.match(message, /world-writable: lib\/python3\.12\/site-packages\/pkg\/mod\.py/);
  });

  it("names bytecode or another entry the prune should have removed", () => {
    assert.match(
      messageFor([...good, "lib/python3.12/site-packages/pkg/__pycache__/mod.cpython-312.pyc"]),
      /left over: lib\/python3\.12\/site-packages\/pkg\/__pycache__/,
    );
    assert.match(
      messageFor([...good, "lib/python3.12/site-packages/.lock"]),
      /left over: lib\/python3\.12\/site-packages\/\.lock/,
    );
    assert.match(messageFor([...good, "lib/libpython3.12.so.1.0"]), /left over: lib\/libpython3\.12\.so\.1\.0/);
  });

  it("names a symlink that dangles, is absolute or leaves the payload", () => {
    assert.match(messageFor([...good, "lib/link-a -> nowhere"]), /dangling symlink: lib\/link-a -> nowhere/);
    assert.match(
      messageFor([...good, "lib/link-b -> /usr/bin/env"]),
      /absolute symlink: lib\/link-b -> \/usr\/bin\/env/,
    );
    assert.match(
      messageFor([...good, "lib/link-c -> ../../outside"], (root) => lay(dirname(root), ["outside"])),
      /symlink leaves the payload: lib\/link-c -> \.\.\/\.\.\/outside/,
    );
  });
});

describe("buildAtomically", () => {
  let umask;
  beforeEach(() => {
    umask = process.umask(0o022);
  });
  afterEach(() => {
    process.umask(umask);
  });

  it("puts the finished tree at the final path and leaves nothing else behind", async () => {
    const final = join(scratch, "payload", "linux-x64");
    await buildAtomically(final, (work) => lay(work, ["bin/python3.12"]));
    assert.deepEqual(listing(final), ["bin", "bin/python3.12"]);
    assert.deepEqual(readdirSync(dirname(final)), ["linux-x64"]);
    assert.equal(statSync(final).mode & 0o777, 0o755, "readable by the user that runs the packaged app");
  });

  it("replaces an earlier payload", async () => {
    const final = join(scratch, "payload", "linux-x64");
    lay(final, ["old.txt"]);
    await buildAtomically(final, (work) => lay(work, ["new.txt"]));
    assert.deepEqual(listing(final), ["new.txt"]);
    assert.deepEqual(readdirSync(dirname(final)), ["linux-x64"]);
  });

  it("leaves no payload at the final path when the build fails, and keeps an earlier one", async () => {
    const parent = join(scratch, "payload");
    const broken = async (work) => {
      lay(work, ["bin/python3.12"]);
      throw new BuildError("the build broke");
    };
    await assert.rejects(buildAtomically(join(parent, "mac-x64"), broken), /the build broke/);
    assert.deepEqual(readdirSync(parent), [], "a failed first build leaves nothing, not even a work folder");

    const final = join(parent, "linux-x64");
    lay(final, ["old.txt"]);
    await assert.rejects(buildAtomically(final, broken), /the build broke/);
    assert.deepEqual(listing(final), ["old.txt"]);
    assert.deepEqual(readdirSync(parent), ["linux-x64"]);
  });

  it("removes what a killed run of the same target left, but not another target's or a live run's", async () => {
    const parent = join(scratch, "payload");
    const gone = spawnSync(process.execPath, ["-e", ""]).pid; // a process id that is free again
    const leftovers = [`.build-linux-x64-${gone}-0123abcd`, `.old-linux-x64-${gone}-0123abcd`];
    const kept = [`.build-mac-x64-${gone}-0123abcd`, `.build-linux-x64-${process.pid}-0123abcd`];
    lay(
      parent,
      [...leftovers, ...kept].map((dir) => `${dir}/partial`),
    );
    await buildAtomically(join(parent, "linux-x64"), (work) => lay(work, ["bin/python3.12"]));
    assert.deepEqual(readdirSync(parent).sort(), [...kept.sort(), "linux-x64"]);
  });
});

describe("failureReason", () => {
  it("gives the error line of uv with the cause line after it", () => {
    const output = [
      "Using CPython 3.14.8",
      "Resolved 15 packages in 3ms",
      "error: Failed to download `idna==3.20`",
      "  cause: Hash mismatch for `idna==3.20`",
      "",
      "         Expected:",
      "           sha256:00",
    ].join("\n");
    assert.equal(
      failureReason(output),
      "error: Failed to download `idna==3.20`; cause: Hash mismatch for `idna==3.20`",
    );
  });

  it("gives the last error line alone when no cause follows, else the last line, else a placeholder", () => {
    assert.equal(failureReason("error: first\nnoise\nError: second\nmore noise"), "Error: second");
    assert.equal(failureReason("tar: Error is not recoverable\ntar: exiting now\n"), "tar: exiting now");
    assert.equal(failureReason("  \n"), "no output");
  });
});

describe("the age of the release", () => {
  it("counts whole days from the tag date", () => {
    assert.equal(pbsAgeDays("20261003", new Date("2026-10-20T12:00:00Z")), 17);
    assert.equal(pbsAgeDays("20261003", new Date("2026-10-03T00:00:00Z")), 0);
    assert.equal(pbsAgeDays("20261003", new Date("2026-11-17T23:59:59Z")), 45);
  });

  it("warns above 45 days and not at 45", () => {
    assert.equal(ageWarning(45), null);
    assert.match(ageWarning(46), /20261003 is 46 days old/);
  });

  it("refuses a tag that is not a date", () => {
    for (const tag of ["2026-10-03", "20261303", "20260230", "latest"]) {
      assert.throws(() => pbsAgeDays(tag), BuildError, tag);
    }
  });
});

describe("the uv and tar arguments", () => {
  /** The value that follows `flag`, which must be there. */
  function valueOf(args, flag) {
    const at = args.indexOf(flag);
    assert.notEqual(at, -1, `${flag} is missing from: ${args.join(" ")}`);
    return args[at + 1];
  }

  it("exports the runtime dependencies from uv.lock as it is, hashes included", () => {
    const args = uvExportArgs();
    assert.equal(args[0], "export");
    for (const flag of ["--frozen", "--no-dev", "--no-emit-project"]) assert.ok(args.includes(flag), flag);
    assert.equal(valueOf(args, "--format"), "requirements.txt");
    assert.ok(!args.includes("--no-hashes"), "the hashes stay in the output");
  });

  it("installs only hashed wheels, copied, for the target's platform and Python 3.12", () => {
    const triples = [
      ["linux-x64", "x86_64-unknown-linux-gnu"],
      ["mac-arm64", "aarch64-apple-darwin"],
      ["mac-x64", "x86_64-apple-darwin"],
    ];
    for (const [target, triple] of triples) {
      const args = uvInstallArgs(target, "/work/site");
      assert.deepEqual(args.slice(0, 2), ["pip", "install"], target);
      assert.equal(valueOf(args, "--link-mode"), "copy", target);
      assert.equal(valueOf(args, "--target"), "/work/site", target);
      assert.equal(valueOf(args, "--python-platform"), triple, target);
      assert.equal(valueOf(args, "--python-version"), "3.12", target);
      assert.equal(valueOf(args, "--only-binary"), ":all:", target);
      assert.equal(valueOf(args, "-r"), "-", `${target}: the requirements come on stdin`);
      for (const flag of ["--require-hashes", "--no-python-downloads"]) assert.ok(args.includes(flag), flag);
    }
  });

  it("names an interpreter for uv only when it is given one", () => {
    assert.ok(!uvInstallArgs("mac-arm64", "/work/site").includes("--python"));
    assert.ok(!uvInstallArgs("mac-arm64", "/work/site", null).includes("--python"));
    assert.equal(
      valueOf(uvInstallArgs("linux-x64", "/work/site", "/work/bin/python3.12"), "--python"),
      "/work/bin/python3.12",
    );
  });

  it("unpacks the archive without its top-level folder, into the folder it is given", () => {
    assert.deepEqual(tarExtractArgs("/cache/a.tar.gz", "/work"), [
      "-xzf",
      "/cache/a.tar.gz",
      "--strip-components=1",
      "-C",
      "/work",
    ]);
  });

  it("can run a target's interpreter only on the platform and CPU it was built for", () => {
    assert.equal(canRunTarget("linux-x64", "linux", "x64"), true);
    assert.equal(canRunTarget("linux-x64", "linux", "arm64"), false);
    assert.equal(canRunTarget("linux-x64", "darwin", "x64"), false);
    assert.equal(canRunTarget("mac-arm64", "darwin", "arm64"), true);
    assert.equal(canRunTarget("mac-arm64", "darwin", "x64"), false);
    assert.equal(canRunTarget("mac-x64", "darwin", "x64"), true);
    // Rosetta would run it on an Apple silicon Mac; the script does not rely on that.
    assert.equal(canRunTarget("mac-x64", "darwin", "arm64"), false);
    assert.equal(canRunTarget("mac-x64", "linux", "x64"), false);
  });
});

describe("running uv", () => {
  const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", "..");
  let savedPath;

  // A uv that writes down how it was called (directory, environment, arguments, standard input) and then prints a
  // requirements file, or, by STUB_UV_MODE, fails the way uv does: finding no Python, a hash mismatch, an error
  // after some output on stdout, or being killed.
  const stubUv = [
    "#!/bin/sh",
    "{",
    '  echo "cwd=$PWD"',
    '  echo "bytecode=${PYTHONDONTWRITEBYTECODE-unset}"',
    '  for arg in "$@"; do echo "arg=$arg"; done',
    '  echo "stdin-follows"',
    "  cat",
    '} > "$STUB_UV_LOG"',
    'case "$STUB_UV_MODE" in',
    '  no-interpreter) echo "error: No interpreter found for Python 3.12 in the system path" >&2; exit 2 ;;',
    "  hash-mismatch) echo 'error: Failed to download `idna==3.20`' >&2; echo '  cause: Hash mismatch' >&2; exit 2 ;;",
    '  partial) echo "idna==3.20"; echo "error: the lock file needs to be updated" >&2; exit 1 ;;',
    "  killed) kill -9 $$ ;;",
    "esac",
    'echo "idna==3.20 \\\\"',
    'echo "    --hash=sha256:00"',
    "",
  ].join("\n");

  beforeEach(() => {
    const bin = join(scratch, "stub-bin");
    mkdirSync(bin);
    writeFileSync(join(bin, "uv"), stubUv, { mode: 0o755 });
    savedPath = process.env.PATH;
    process.env.PATH = `${bin}${delimiter}${savedPath}`;
    process.env.STUB_UV_LOG = join(scratch, "uv-call.txt");
  });
  afterEach(() => {
    process.env.PATH = savedPath;
    delete process.env.STUB_UV_LOG;
    delete process.env.STUB_UV_MODE;
  });

  /** What the stand-in uv wrote down about its last call. */
  function call() {
    const lines = readFileSync(process.env.STUB_UV_LOG, "utf8").split("\n");
    const field = (name) => lines.find((line) => line.startsWith(`${name}=`))?.slice(name.length + 1);
    return {
      cwd: field("cwd"),
      bytecode: field("bytecode"),
      args: lines.filter((line) => line.startsWith("arg=")).map((line) => line.slice(4)),
      stdin: lines.slice(lines.indexOf("stdin-follows") + 1).join("\n"),
    };
  }

  /** A work folder as the archive leaves it: site-packages holding pip and two installed packages. */
  function workFolder() {
    const work = join(scratch, "work");
    lay(work, [
      "lib/python3.12/site-packages/idna/__init__.py",
      "lib/python3.12/site-packages/idna-3.20.dist-info/METADATA",
      "lib/python3.12/site-packages/yaml-6.0.dist-info/METADATA",
      "lib/python3.12/site-packages/pip-26.2.1.dist-info/METADATA",
    ]);
    return work;
  }

  it("exports from the repository root with uvExportArgs and returns what uv prints", async () => {
    const text = await exportRequirements();
    const seen = call();
    assert.deepEqual(seen.args, uvExportArgs());
    assert.equal(realpathSync(seen.cwd), realpathSync(repoRoot));
    assert.equal(text, "idna==3.20 \\\n    --hash=sha256:00\n");
  });

  it("installs from the temp folder with the requirements on stdin and no bytecode, and counts the packages", async () => {
    const work = workFolder();
    const site = join(work, "lib/python3.12/site-packages");
    const requirements = "idna==3.20 \\\n    --hash=sha256:00\n";

    const packages = await installDependencies("linux-x64", work, requirements);

    const seen = call();
    const python = canRunTarget("linux-x64") ? join(work, "bin/python3.12") : null;
    assert.deepEqual(seen.args, uvInstallArgs("linux-x64", site, python));
    assert.equal(seen.stdin, requirements);
    assert.equal(seen.bytecode, "1", "uv's query of the payload's interpreter must not write .pyc files");
    assert.equal(
      realpathSync(seen.cwd),
      realpathSync(tmpdir()),
      "not the repository, whose pyproject.toml uv would read",
    );
    assert.equal(packages, 2, "pip's own dist-info is not a locked package");
  });

  it("leaves the choice of interpreter to uv for a target that this machine cannot run", async () => {
    const other = process.platform === "darwin" ? "linux-x64" : "mac-arm64";
    await installDependencies(other, workFolder(), "x\n");
    assert.ok(!call().args.includes("--python"));
  });

  it("turns uv's failure into one line, with what to do when it found no Python", async () => {
    process.env.STUB_UV_MODE = "no-interpreter";
    await assert.rejects(
      installDependencies("mac-arm64", workFolder(), "x\n"),
      (err) =>
        err instanceof BuildError &&
        !err.message.includes("\n") &&
        /^uv pip install failed \(exit 2\): error: No interpreter found for Python 3\.12.*UV_PYTHON/.test(err.message),
    );
    await assert.rejects(exportRequirements(), /^BuildError: uv export failed \(exit 2\): error: No interpreter found/);
  });

  it("gives uv's own reason, cause line included, without Python advice, when it fails for another reason", async () => {
    process.env.STUB_UV_MODE = "hash-mismatch";
    await assert.rejects(
      installDependencies("linux-x64", workFolder(), "x\n"),
      (err) =>
        err instanceof BuildError &&
        err.message === "uv pip install failed (exit 2): error: Failed to download `idna==3.20`; cause: Hash mismatch",
    );
  });

  it("reads the reason from stderr when the program printed on stdout first, and names the signal that killed it", async () => {
    process.env.STUB_UV_MODE = "partial";
    await assert.rejects(exportRequirements(), /^BuildError: uv export failed \(exit 1\): error: the lock file needs/);
    process.env.STUB_UV_MODE = "killed";
    await assert.rejects(exportRequirements(), /^BuildError: uv export failed \(signal SIGKILL\): no output$/);
  });

  it("says so when uv is not installed", async () => {
    process.env.PATH = join(scratch, "no-programs");
    await assert.rejects(
      exportRequirements(),
      (err) => err instanceof BuildError && err.message === "uv was not found on PATH",
    );
  });
});

describe("extract", () => {
  function archive() {
    const source = join(scratch, "source");
    lay(source, ["python/bin/python3.12", "python/lib/python3.12/os.py"]);
    const file = join(scratch, "archive.tar.gz");
    const made = spawnSync("tar", ["-czf", file, "-C", source, "python"], { encoding: "utf8" });
    assert.equal(made.status, 0, made.stderr);
    return file;
  }

  it("unpacks the files of the archive's top-level folder into the work folder", async () => {
    const work = join(scratch, "work");
    mkdirSync(work);
    await extract(archive(), work);
    assert.deepEqual(listing(work), ["bin", "bin/python3.12", "lib", "lib/python3.12", "lib/python3.12/os.py"]);
  });

  it("reports a file that is not an archive in one line", async () => {
    const work = join(scratch, "work");
    mkdirSync(work);
    const file = join(scratch, "not-an-archive.tar.gz");
    writeFileSync(file, "this is not a tar file\n");
    await assert.rejects(
      extract(file, work),
      (err) => err instanceof BuildError && /^tar failed \(exit \d+\)/.test(err.message) && !err.message.includes("\n"),
    );
    assert.deepEqual(listing(work), []);
  });
});

// The network is replaced where the script calls it: the global fetch. A local https server would need a
// certificate that fetch accepts, and nothing in this package provides a mock server for tests.
describe("downloadFile", () => {
  const url = "https://github.com/astral-sh/x/releases/download/1/archive.tar.gz";
  const where = "github.com/astral-sh/x/releases/download/1/archive.tar.gz";
  let dest;
  beforeEach(() => {
    dest = join(scratch, "archive.tar.gz.part-1");
  });
  afterEach(() => {
    mock.restoreAll();
  });

  /** Stands in for fetch: `handler(url, init)` makes the response. Returns the calls it received. */
  function serve(handler) {
    const seen = [];
    mock.method(globalThis, "fetch", async (input, init) => {
      seen.push({ url: String(input), init });
      return handler(String(input), init);
    });
    return seen;
  }
  const redirect = (location, body = null) => new Response(body, { status: 302, headers: { location } });

  it("writes the body of the response to the file", async () => {
    const seen = serve(() => new Response("archive bytes"));
    await downloadFile(url, dest);
    assert.equal(readFileSync(dest, "utf8"), "archive bytes");
    assert.deepEqual(
      seen.map((call) => call.url),
      [url],
    );
  });

  it("times a download out after minutes, not never and not at once", () => {
    assert.ok(DOWNLOAD_TIMEOUT_MS >= 60_000 && DOWNLOAD_TIMEOUT_MS <= 3_600_000, `${DOWNLOAD_TIMEOUT_MS} ms`);
  });

  it("gives fetch an AbortSignal.timeout of DOWNLOAD_TIMEOUT_MS, and handles redirects itself", async () => {
    const timeout = mock.method(AbortSignal, "timeout"); // records the calls, still makes the real signal
    const seen = serve(() => new Response("x"));
    await downloadFile(url, dest);
    const { init } = seen[0];
    assert.ok(init.signal instanceof AbortSignal && !init.signal.aborted);
    assert.deepEqual(
      timeout.mock.calls.map((call) => call.arguments),
      [[DOWNLOAD_TIMEOUT_MS]],
    );
    assert.equal(init.redirect, "manual", "so that every redirect can be checked to be https");
  });

  it(
    "gives up on a download that stalls, after the timeout it was given, and leaves no file",
    { timeout: 5000 },
    async () => {
      serve(
        (_url, init) =>
          new Promise((_resolve, reject) => {
            if (!init.signal) reject(new Error("fetch was called without a signal"));
            else init.signal.addEventListener("abort", () => reject(init.signal.reason));
          }),
      );
      await assert.rejects(
        downloadFile(url, dest, 50),
        (err) =>
          err instanceof BuildError && new RegExp(`^download timed out after \\d+ s: ${where}$`).test(err.message),
      );
      assert.ok(!existsSync(dest));
    },
  );

  it("follows redirects to another https address, absolute or relative, and writes the final body", async () => {
    let released = 0;
    const body = () => new ReadableStream({ cancel: () => void (released += 1) });
    const seen = serve((from) => {
      if (from === url) return redirect("https://objects.example.com/blob?sig=secret", body());
      if (from.startsWith("https://objects.example.com/blob")) return redirect("/elsewhere/blob", body());
      return new Response("final bytes");
    });
    await downloadFile(url, dest);
    assert.equal(released, 2, "the body of each redirect response is released");
    assert.deepEqual(
      seen.map((call) => call.url),
      [url, "https://objects.example.com/blob?sig=secret", "https://objects.example.com/elsewhere/blob"],
    );
    assert.equal(readFileSync(dest, "utf8"), "final bytes");
  });

  it("refuses a redirect to http, without the query in the message, and writes nothing", async () => {
    // What fetch itself does when it is left to follow redirects: it would fetch the http address.
    serve((_from, init) =>
      init.redirect === "manual"
        ? redirect("http://mirror.example.com/blob?sig=secret")
        : new Response("bytes that came over http"),
    );
    await assert.rejects(
      downloadFile(url, dest),
      (err) =>
        err instanceof BuildError &&
        err.message === "refusing a http URL, only https is allowed: mirror.example.com/blob",
    );
    assert.ok(!existsSync(dest));
  });

  it("stops after five redirects", async () => {
    const seen = serve(() => redirect(url));
    await assert.rejects(downloadFile(url, dest), /more than 5 redirects from github\.com/);
    assert.equal(seen.length, 6);
  });

  it("reports an HTTP error status and leaves no file", async () => {
    serve(() => new Response("not found", { status: 404 }));
    await assert.rejects(downloadFile(url, dest), new RegExp(`^BuildError: download failed with HTTP 404: ${where}$`));
    assert.ok(!existsSync(dest));
  });

  it("reports a failed connection by its code", async () => {
    serve(() => {
      throw Object.assign(new TypeError("fetch failed"), { cause: { code: "ECONNREFUSED" } });
    });
    await assert.rejects(
      downloadFile(url, dest),
      new RegExp(`^BuildError: download failed \\(ECONNREFUSED\\): ${where}$`),
    );
  });

  it("removes the partial file when the body breaks off half way", async () => {
    const body = new ReadableStream({
      start: (controller) => controller.enqueue(new TextEncoder().encode("part")),
      pull: (controller) => controller.error(new Error("socket hang up")),
    });
    serve(() => new Response(body));
    await assert.rejects(
      downloadFile(url, dest),
      new RegExp(`^BuildError: download failed \\(socket hang up\\): ${where}$`),
    );
    assert.ok(!existsSync(dest));
  });
});

describe("fetchVerified", () => {
  const bytes = Buffer.from("pretend archive ".repeat(500));
  const sha256 = createHash("sha256").update(bytes).digest("hex");
  const url = "https://example.com/dist/archive.tar.gz";
  let file;
  let downloads;
  let said;
  beforeEach(() => {
    file = join(scratch, "downloads", "archive.tar.gz");
    downloads = [];
    said = [];
  });

  /** A download that writes `content` to the part file, and notes where the final file stood at that moment. */
  const writes = (content) => async (from, to) => {
    downloads.push({ from, to, finalExisted: existsSync(file) });
    writeFileSync(to, content);
  };
  const fetchIt = (download) => fetchVerified({ url, file, sha256, download, report: (message) => said.push(message) });
  const folder = () => readdirSync(dirname(file)).sort();

  it("downloads into a part file beside the final one and moves it there once its hash matches", async () => {
    assert.equal(await fetchIt(writes(bytes)), file);
    assert.equal(downloads.length, 1);
    assert.equal(downloads[0].from, url);
    assert.equal(dirname(downloads[0].to), dirname(file));
    assert.match(downloads[0].to, /archive\.tar\.gz\.part-\d+$/);
    assert.equal(downloads[0].finalExisted, false);
    assert.deepEqual(readFileSync(file), bytes);
    assert.deepEqual(folder(), ["archive.tar.gz"]);
    assert.match(said.join("\n"), /downloaded archive\.tar\.gz \(.* MiB\) in .* s and verified its sha256/);
  });

  it("keeps a cached file whose hash matches and downloads nothing", async () => {
    mkdirSync(dirname(file));
    writeFileSync(file, bytes);
    assert.equal(await fetchIt(writes(Buffer.from("must not be used"))), file);
    assert.equal(downloads.length, 0);
    assert.deepEqual(readFileSync(file), bytes);
    assert.match(said.join("\n"), /archive\.tar\.gz is in the cache and matches its sha256/);
  });

  it("deletes a cached file whose hash does not match, says so, and downloads it again", async () => {
    mkdirSync(dirname(file));
    writeFileSync(file, "a damaged download");
    await fetchIt(writes(bytes));
    assert.equal(downloads.length, 1);
    assert.equal(downloads[0].finalExisted, false, "the damaged file was gone before the download began");
    assert.deepEqual(readFileSync(file), bytes);
    assert.match(said[0], /cached archive\.tar\.gz did not match its sha256 and was deleted/);
  });

  it("refuses a download whose hash does not match: no file at the final path, no part file", async () => {
    await assert.rejects(fetchIt(writes(Buffer.from("something else"))), /sha256 mismatch/);
    assert.deepEqual(folder(), []);
  });

  it("leaves no part file when the download itself fails half way", async () => {
    const dies = async (_from, to) => {
      writeFileSync(to, bytes.subarray(0, 100));
      throw new BuildError("connection reset");
    };
    await assert.rejects(fetchIt(dies), /connection reset/);
    assert.deepEqual(folder(), []);
  });

  it("removes the part files of runs that are gone, but not those of a run that is going or of another file", async () => {
    const gone = spawnSync(process.execPath, ["-e", ""]).pid; // a process id that is free again
    const going = process.ppid; // the process that started this one is still there
    const parts = [`archive.tar.gz.part-${gone}`, `archive.tar.gz.part-${going}`, `other.tar.gz.part-${gone}`];
    lay(dirname(file), parts);
    await fetchIt(writes(bytes));
    assert.deepEqual(folder(), ["archive.tar.gz", `archive.tar.gz.part-${going}`, `other.tar.gz.part-${gone}`]);
  });

  it("needs no report function", async () => {
    await fetchVerified({ url, file, sha256, download: writes(bytes) });
    assert.deepEqual(readFileSync(file), bytes);
  });
});
