import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { readEvents } from "../../app/aihub_web/static/js/api.js";
import { firstChange, hasClosingFence, resolveLanguage } from "../../app/aihub_web/static/js/markdown-core.js";

const bytes = (text) => new TextEncoder().encode(text);

/** A reader that hands back the given chunks one at a time, like a network stream. */
function reader(chunks) {
  const queue = [...chunks];
  return {
    read: async () => (queue.length ? { value: queue.shift(), done: false } : { value: undefined, done: true }),
  };
}

async function collect(chunks) {
  const events = [];
  for await (const event of readEvents(reader(chunks))) events.push(event);
  return events;
}

describe("readEvents", () => {
  const lines = ['{"t":"text","d":"Hel"}', '{"t":"text","d":"lo"}', '{"t":"done"}'];
  const all = lines.join("\n") + "\n";

  it("reads whole lines", async () => {
    assert.deepEqual(
      (await collect([bytes(all)])).map((e) => e.t),
      ["text", "text", "done"],
    );
  });

  it("reads the same events however the network splits the bytes", async () => {
    const data = bytes(all);
    for (let size = 1; size <= data.length; size++) {
      const chunks = [];
      for (let at = 0; at < data.length; at += size) chunks.push(data.slice(at, at + size));
      const events = await collect(chunks);
      assert.deepEqual(
        events.map((e) => e.d ?? e.t),
        ["Hel", "lo", "done"],
        `chunk size ${size}`,
      );
    }
  });

  it("keeps a multi-byte character that is split between two chunks", async () => {
    const data = bytes('{"t":"text","d":"café ✓ 你好"}\n');
    for (let cut = 1; cut < data.length; cut++) {
      const [event] = await collect([data.slice(0, cut), data.slice(cut)]);
      assert.equal(event.d, "café ✓ 你好", `cut at ${cut}`);
    }
  });

  it("reads a last line that has no newline, and skips blank lines", async () => {
    const events = await collect([bytes('\n{"t":"a"}\n\n{"t":"b"}')]);
    assert.deepEqual(
      events.map((e) => e.t),
      ["a", "b"],
    );
  });

  it("yields events as they arrive, before the stream ends", async () => {
    let release;
    const gate = new Promise((resolve) => (release = resolve));
    const slow = {
      step: 0,
      async read() {
        if (this.step++ === 0) return { value: bytes('{"t":"first"}\n'), done: false };
        await gate;
        return { value: undefined, done: true };
      },
    };
    const iterator = readEvents(slow);
    assert.equal((await iterator.next()).value.t, "first");
    release();
    assert.equal((await iterator.next()).done, true);
  });

  it("throws on a line that is not JSON instead of dropping it", async () => {
    await assert.rejects(collect([bytes('{"t":"ok"}\nnot json\n')]), SyntaxError);
  });
});

describe("firstChange: which streamed blocks to redo", () => {
  it("keeps everything before the first block that differs", () => {
    assert.equal(firstChange(["a", "b", "c"], ["a", "b", "c", "d"]), 3);
    assert.equal(firstChange(["a", "b", "c"], ["a", "x", "c"]), 1);
    assert.equal(firstChange(["a", "b"], ["a"]), 1);
    assert.equal(firstChange([], ["a"]), 0);
    assert.equal(firstChange(["a"], ["a"]), 1);
  });
});

describe("hasClosingFence: when a code block may be highlighted", () => {
  it("is false while the fence is still open", () => {
    assert.equal(hasClosingFence("```python\nprint(1)"), false);
    assert.equal(hasClosingFence("```python\n"), false);
    assert.equal(hasClosingFence("```"), false);
    assert.equal(hasClosingFence("~~~\ncode\n~~"), false);
  });
  it("is true once it closes, with any fence length", () => {
    assert.equal(hasClosingFence("```python\nprint(1)\n```"), true);
    assert.equal(hasClosingFence("```python\nprint(1)\n```\n"), true);
    assert.equal(hasClosingFence("````\ncode\n````"), true);
    assert.equal(hasClosingFence("~~~\ncode\n~~~"), true);
  });
  it("is true for anything that is not a fenced block", () => {
    assert.equal(hasClosingFence("    indented code"), true);
    assert.equal(hasClosingFence("plain paragraph"), true);
  });
});

describe("resolveLanguage", () => {
  it("maps common names and aliases to the files we ship", () => {
    const cases = {
      python: "python",
      PY: "python",
      js: "javascript",
      "language-ts": "typescript",
      sh: "bash",
      "c++": "cpp",
      html: "xml",
      yml: "yaml",
      toml: "ini",
      "python title=x": "python",
    };
    for (const [info, expected] of Object.entries(cases)) assert.equal(resolveLanguage(info), expected, info);
  });
  it("returns null for languages we do not ship, so the block stays plain", () => {
    for (const info of ["", null, undefined, "brainfuck", "../../etc/passwd", "python/../../x"]) {
      assert.equal(resolveLanguage(info), info === "python/../../x" ? null : null, String(info));
    }
  });
});
