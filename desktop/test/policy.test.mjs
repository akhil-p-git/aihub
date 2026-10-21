// Run inside desktop/ with: npm test
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { allowsRequest, buildChildEnv, externalTarget, isBackendUrl, menuTemplate } from "../src/policy.js";

const ORIGIN = "http://127.0.0.1:41234";

const NOT_STRINGS = [
  undefined,
  null,
  0,
  1n,
  true,
  {},
  [],
  () => {},
  Symbol("url"),
  new URL(ORIGIN),
  { href: ORIGIN },
  {
    toString() {
      throw new Error("a policy function must not call toString");
    },
  },
];

const GARBAGE = [
  "",
  " ",
  "\n",
  "\0",
  "not a url",
  "http://",
  "http:",
  "://",
  "//",
  "http://[",
  "http://%",
  "http://127.0.0.1:99999/",
  "http://127.0.0.1:41234:80/",
  "\ud800",
  "%".repeat(100_000),
  "a".repeat(1_000_000),
];

describe("isBackendUrl", () => {
  const backend = [
    [ORIGIN, "the origin without a path"],
    [`${ORIGIN}/`, "the origin with a slash"],
    [`${ORIGIN}/api/health`, "an API path"],
    [`${ORIGIN}/#/c/0123456789abcdef`, "a hash route"],
    [`${ORIGIN}/?q=a&b=c`, "a query"],
    ["HTTP://127.0.0.1:41234/", "an uppercase scheme"],
    ["http://127.0.0.1:041234/", "a port with a leading zero"],
    ["http://evil.example@127.0.0.1:41234/", "userinfo in front of the real host"],
    ["http://127.0.0.1:41234/\\@evil.example", "a backslash, which starts the path"],
    ["http://127.0.0.1.:41234/", "a trailing dot on an IPv4 address, which the URL parser drops"],
    ["http://127.1:41234/", "the same IPv4 address spelled short"],
  ];
  for (const [url, what] of backend) {
    it(`accepts ${what}`, () => assert.equal(isBackendUrl(url, ORIGIN), true, url));
  }

  const elsewhere = [
    ["http://127.0.0.1:41235/", "another port"],
    ["http://127.0.0.1:8787/", "the service's port"],
    ["http://127.0.0.1/", "port 80"],
    ["https://127.0.0.1:41234/", "https against an http origin"],
    ["http://localhost:41234/", "localhost, which is another origin string"],
    ["http://localhost.:41234/", "localhost with a trailing dot"],
    ["http://[::1]:41234/", "the IPv6 loopback"],
    ["http://[::ffff:127.0.0.1]:41234/", "an IPv4-mapped IPv6 address"],
    ["http://127.0.0.2:41234/", "another loopback address"],
    ["http://0.0.0.0:41234/", "the unspecified address"],
    ["http://127.0.0.1:41234@evil.example/", "userinfo that looks like the origin"],
    ["http://127.0.0.1:41234%40evil.example/", "an encoded @ in the port"],
    ["http://127.0.0.1.evil.example:41234/", "a look-alike host"],
    ["http://127.0.0.1%2eevil.example:41234/", "a look-alike host with an encoded dot"],
    ["http://127.0.0.1:41234.evil.example/", "a look-alike host after the port"],
    ["http://evil.example/?u=http://127.0.0.1:41234/", "the origin in a query"],
    ["http://evil.example/#http://127.0.0.1:41234/", "the origin in a fragment"],
    ["http://evil.example:41234/", "the port on another host"],
    ["blob:http://127.0.0.1:41234/6f1e1f0c", "a blob URL, whose origin is the backend's"],
    ["filesystem:http://127.0.0.1:41234/temporary/x", "a filesystem URL"],
    ["ws://127.0.0.1:41234/", "a WebSocket URL"],
    ["view-source:http://127.0.0.1:41234/", "view-source"],
    ["javascript:void(0)", "a javascript URL"],
    ["data:text/html,<p>x</p>", "a data URL"],
    ["file:///etc/passwd", "a file URL"],
    ["about:blank", "about:blank"],
    ["chrome://gpu", "a chrome URL"],
    ["devtools://devtools/bundled/inspector.html", "a devtools URL"],
    ["//127.0.0.1:41234/", "a scheme-less URL"],
    ["127.0.0.1:41234", "a bare host and port"],
    ["/api/health", "a relative path"],
  ];
  for (const [url, what] of elsewhere) {
    it(`refuses ${what}`, () => assert.equal(isBackendUrl(url, ORIGIN), false, url));
  }

  it("refuses an origin that is not exactly an origin, rather than guessing", () => {
    for (const origin of [`${ORIGIN}/`, `${ORIGIN} `, "HTTP://127.0.0.1:41234", "", "null", "127.0.0.1:41234"]) {
      assert.equal(isBackendUrl(`${ORIGIN}/`, origin), false, origin);
    }
  });

  it("never lets an opaque origin match 'null'", () => {
    for (const url of ["about:blank", "data:text/html,x", "file:///x", "javascript:0", "blob:null/1"]) {
      assert.equal(isBackendUrl(url, "null"), false, url);
    }
  });

  it("answers false, without throwing, for garbage and for non-strings", () => {
    for (const url of [...GARBAGE, ...NOT_STRINGS]) assert.equal(isBackendUrl(url, ORIGIN), false);
    for (const origin of [...GARBAGE, ...NOT_STRINGS]) assert.equal(isBackendUrl(`${ORIGIN}/`, origin), false);
  });

  it("copes with a very long path on the backend and a very long host elsewhere", () => {
    assert.equal(isBackendUrl(`${ORIGIN}/${"a".repeat(1_000_000)}`, ORIGIN), true);
    assert.equal(isBackendUrl(`http://${"a".repeat(1_000_000)}/`, ORIGIN), false);
  });
});

describe("externalTarget", () => {
  const opens = [
    ["https://example.com/", "https://example.com/"],
    ["https://example.com/a/b?c=d#e", "https://example.com/a/b?c=d#e"],
    ["http://example.com", "http://example.com/"],
    ["HTTPS://EXAMPLE.COM/Path", "https://example.com/Path"],
    ["https://bücher.example/", "https://xn--bcher-kva.example/"],
    ["https://exam\tple.com/\n", "https://example.com/"], // the parser drops tabs and newlines; the clean form is returned
    [" https://example.com ", "https://example.com/"],
    ["http://127.0.0.1:41235/", "http://127.0.0.1:41235/"], // the service or any other local server
    ["http://127.0.0.1:8787/", "http://127.0.0.1:8787/"],
    ["https://127.0.0.1:41234/", "https://127.0.0.1:41234/"], // https is another origin than the http backend
    ["http://[::1]:41234/", "http://[::1]:41234/"],
    ["http://127.0.0.1:41234@evil.example/", "http://127.0.0.1:41234@evil.example/"], // a link to evil.example, not the backend
    ["http://127.0.0.1.evil.example:41234/", "http://127.0.0.1.evil.example:41234/"],
    ["mailto:someone@example.com", "mailto:someone@example.com"],
    ["MAILTO:someone@example.com", "mailto:someone@example.com"],
  ];
  for (const [url, expected] of opens) {
    it(`opens ${JSON.stringify(url)}`, () => assert.equal(externalTarget(url, ORIGIN), expected));
  }

  const backend = [
    ORIGIN,
    `${ORIGIN}/`,
    `${ORIGIN}/api/chats`,
    `${ORIGIN}/#/c/0123456789abcdef`,
    "HTTP://127.0.0.1:41234/x",
    "http://evil.example@127.0.0.1:41234/",
    "http://127.0.0.1.:41234/",
  ];
  for (const url of backend) {
    it(`never opens the backend itself: ${url}`, () => assert.equal(externalTarget(url, ORIGIN), null));
  }

  const mailWithHeaders = [
    "mailto:a@b.example?subject=hi",
    "mailto:a@b.example?body=text&cc=c@d.example",
    "mailto:a@b.example?attach=/etc/passwd",
    "mailto:?to=a@b.example",
  ];
  for (const url of mailWithHeaders) {
    it(`refuses a mailto link with a query: ${url}`, () => assert.equal(externalTarget(url, ORIGIN), null));
  }

  const refused = [
    "javascript:alert(1)",
    "JavaScript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
    "vbscript:msgbox(1)",
    "blob:http://127.0.0.1:41234/6f1e1f0c",
    "blob:https://example.com/6f1e1f0c",
    "filesystem:https://example.com/temporary/x",
    "ftp://example.com/file",
    "ws://example.com/",
    "wss://example.com/",
    "ssh://example.com/",
    "tel:+15555550100",
    "sms:+15555550100",
    "smb://server/share",
    "steam://run/10",
    "slack://open",
    "myapp://anything",
    "view-source:https://example.com/",
    "chrome://gpu",
    "devtools://devtools/bundled/inspector.html",
    "about:blank",
    "//example.com/",
    "example.com",
    "/relative/path",
  ];
  for (const url of refused) {
    it(`refuses ${url}`, () => assert.equal(externalTarget(url, ORIGIN), null));
  }

  it("answers null, without throwing, for garbage and for non-strings", () => {
    for (const url of [...GARBAGE, ...NOT_STRINGS]) {
      assert.equal(externalTarget(url, ORIGIN), null, String(typeof url === "string" ? url.slice(0, 40) : typeof url));
    }
  });

  it("does not throw on a very long link", () => {
    const long = `https://example.com/${"a".repeat(1_000_000)}`;
    assert.doesNotThrow(() => externalTarget(long, ORIGIN));
    assert.doesNotThrow(() => externalTarget(`http://${"a".repeat(1_000_000)}`, ORIGIN));
  });

  it("opens http links when the backend's origin is unknown", () => {
    for (const origin of NOT_STRINGS)
      assert.equal(externalTarget("https://example.com/", origin), "https://example.com/");
  });
});

describe("allowsRequest", () => {
  const allowed = [
    [ORIGIN, "the origin without a path"],
    [`${ORIGIN}/`, "the root page"],
    [`${ORIGIN}/api/chat`, "an API path"],
    [`${ORIGIN}/api/chats?limit=20&q=a%20b`, "a query"],
    [`${ORIGIN}/static/js/app.js`, "a script"],
    [`${ORIGIN}/#/c/0123456789abcdef`, "a hash route"],
    [`${ORIGIN}/\\@evil.example`, "a backslash after the origin, which belongs to the path"],
    [`${ORIGIN}//127.0.0.1:41235/`, "another origin written into the path"],
  ];
  for (const [url, what] of allowed) {
    it(`lets through ${what}`, () => assert.equal(allowsRequest(url, ORIGIN), true, url));
  }

  const refused = [
    ["http://127.0.0.1:41235/", "another port"],
    ["http://127.0.0.1:8787/", "the service's port"],
    ["http://127.0.0.1/", "port 80"],
    ["http://localhost:41234/", "localhost, which is another host"],
    ["http://127.0.0.2:41234/", "another loopback address"],
    ["http://[::1]:41234/", "the IPv6 loopback"],
    ["http://0.0.0.0:41234/", "the unspecified address"],
    ["http://example.com/", "another host"],
    ["https://example.com/", "another host over https"],
    ["http://evil.example:41234/", "the port on another host"],
    ["https://127.0.0.1:41234/", "https on the backend's host and port"],
    ["http://evil.example@127.0.0.1:41234/", "userinfo in front of the backend's host"],
    ["http://user:secret@127.0.0.1:41234/", "a user name and password for the backend's host"],
    ["http://127.0.0.1:41234@evil.example/", "userinfo that looks like the origin"],
    ["http://127.0.0.1:41234%40evil.example/", "an encoded @ after the port"],
    ["http://127.0.0.1.evil.example:41234/", "a look-alike host"],
    ["http://127.0.0.1:41234.evil.example/", "a look-alike host after the port"],
    ["http://evil.example/?u=http://127.0.0.1:41234/", "the origin in a query"],
    ["http://evil.example/#http://127.0.0.1:41234/", "the origin in a fragment"],
    ["ws://127.0.0.1:41234/", "a WebSocket on the backend's host and port"],
    ["wss://127.0.0.1:41234/", "a secure WebSocket on the backend's host and port"],
    ["ws://127.0.0.1:41234/api/chat", "a WebSocket on an API path"],
    ["ws://example.com/", "a WebSocket elsewhere"],
    ["HTTP://127.0.0.1:41234/", "an uppercase scheme on the backend's host and port"],
    ["Http://127.0.0.1:41234/api/chat", "a mixed-case scheme"],
    ["HTTP://EVIL.EXAMPLE/", "an uppercase scheme elsewhere"],
    ["WS://127.0.0.1:41234/", "an uppercase WebSocket scheme"],
    ["http://127.0.0.1:041234/", "a port with a leading zero"],
    ["http://127.0.0.1.:41234/", "a trailing dot on the address"],
    ["http://127.1:41234/", "the address written short"],
    ["blob:http://127.0.0.1:41234/6f1e1f0c", "a blob URL, whose origin is the backend's"],
    ["filesystem:http://127.0.0.1:41234/temporary/x", "a filesystem URL"],
    ["view-source:http://127.0.0.1:41234/", "view-source"],
    ["data:text/html,<p>x</p>", "a data URL"],
    ["file:///etc/passwd", "a file URL"],
    ["javascript:void(0)", "a javascript URL"],
    ["about:blank", "about:blank"],
    ["chrome://gpu", "a chrome URL"],
    ["devtools://devtools/bundled/inspector.html", "a devtools URL"],
    ["ftp://127.0.0.1:41234/", "an ftp URL on the backend's host and port"],
    ["//127.0.0.1:41234/", "a scheme-less URL"],
    ["127.0.0.1:41234", "a bare host and port"],
    ["/api/health", "a relative path"],
    ["", "an empty string"],
  ];
  for (const [url, what] of refused) {
    it(`stops ${what}`, () => assert.equal(allowsRequest(url, ORIGIN), false, url));
  }

  // Before the first backend is up, and from the moment it has ended, there is no origin: nothing may go out.
  it("stops everything while there is no origin", () => {
    const urls = [
      ORIGIN,
      `${ORIGIN}/`,
      `${ORIGIN}/api/chat`,
      "http://127.0.0.1:8787/",
      "https://example.com/",
      "ws://x/",
      "",
    ];
    for (const origin of [null, undefined, "", "null", ...NOT_STRINGS, ...GARBAGE]) {
      for (const url of urls) {
        assert.equal(allowsRequest(url, origin), false, `${url} against an origin of type ${typeof origin}`);
      }
    }
  });

  it("stops an origin that is not exactly an origin, rather than guessing", () => {
    for (const origin of [
      `${ORIGIN}/`,
      `${ORIGIN} `,
      ` ${ORIGIN}`,
      "HTTP://127.0.0.1:41234",
      "http://127.0.0.1:041234",
      "http://",
      "127.0.0.1:41234",
      "https://127.0.0.1:41234/x",
    ]) {
      assert.equal(allowsRequest(`${ORIGIN}/`, origin), false, origin);
      assert.equal(allowsRequest(`${origin}/`, origin), false, origin);
    }
  });

  it("answers false, without throwing, for garbage and for non-strings", () => {
    for (const url of [...GARBAGE, ...NOT_STRINGS]) {
      assert.equal(allowsRequest(url, ORIGIN), false, String(typeof url === "string" ? url.slice(0, 40) : typeof url));
    }
  });

  it("copes with a very long path on the backend and a very long host elsewhere", () => {
    assert.equal(allowsRequest(`${ORIGIN}/${"a".repeat(1_000_000)}`, ORIGIN), true);
    assert.equal(allowsRequest(`http://${"a".repeat(1_000_000)}/`, ORIGIN), false);
  });

  it("follows the origin it is given, so a restart on another port moves the allowance", () => {
    const next = "http://127.0.0.1:50000";
    assert.equal(allowsRequest(`${ORIGIN}/api/health`, next), false);
    assert.equal(allowsRequest(`${next}/api/health`, next), true);
    assert.equal(allowsRequest(`${next}/api/health`, ORIGIN), false);
  });
});

describe("isBackendUrl and externalTarget together", () => {
  // Seeded, so a failure reproduces: mulberry32.
  function random(seed) {
    return () => {
      seed = (seed + 0x6d2b79f5) | 0;
      let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }
  const PIECES = [
    "http://",
    "https://",
    "HTTP://",
    "mailto:",
    "javascript:",
    "blob:",
    "data:",
    "file://",
    "127.0.0.1",
    "127.1",
    "127.0.0.1.",
    "localhost",
    "evil.example",
    "[::1]",
    "[::ffff:7f00:1]",
    ":41234",
    ":41235",
    ":",
    "@",
    "/",
    "//",
    "\\",
    "?",
    "?q=1",
    "#",
    "#/c/1",
    "%40",
    "%2e",
    "%00",
    "\t",
    "\n",
    " ",
    "\0",
    "ü",
    "。",
    "a",
    "0x7f",
    "..",
    "-",
    "a@b.example",
  ];

  it("never throws, never calls a URL both the backend and external, and returns only what is safe to open", () => {
    const next = random(20261020);
    for (let i = 0; i < 20_000; i++) {
      const length = 1 + Math.floor(next() * 8);
      let url = "";
      for (let j = 0; j < length; j++) url += PIECES[Math.floor(next() * PIECES.length)];
      const backend = isBackendUrl(url, ORIGIN);
      const target = externalTarget(url, ORIGIN);
      const allowedRequest = allowsRequest(url, ORIGIN);
      assert.equal(typeof backend, "boolean", url);
      assert.ok(target === null || typeof target === "string", url);
      if (backend) assert.equal(target, null, `the backend's own URL must not leave the app: ${JSON.stringify(url)}`);
      assert.equal(allowsRequest(url, null), false, `nothing goes out without an origin: ${JSON.stringify(url)}`);
      if (allowedRequest) {
        const spelled = url === ORIGIN || url.startsWith(`${ORIGIN}/`);
        assert.ok(
          backend && spelled,
          `a request may only go to the backend, spelled as Chromium spells it: ${JSON.stringify(url)}`,
        );
      }
      if (target !== null) {
        const parsed = new URL(target);
        assert.ok(["http:", "https:", "mailto:"].includes(parsed.protocol), target);
        assert.notEqual(parsed.origin, ORIGIN, target);
        if (parsed.protocol === "mailto:") assert.equal(parsed.search, "", target);
        assert.equal(isBackendUrl(target, ORIGIN), false, target);
      }
    }
  });
});

describe("menuTemplate", () => {
  /** Every role and label in the template, at any depth, lower-cased. */
  function names(items) {
    return items.flatMap((item) => [
      ...[item.role, item.label].filter(Boolean).map((name) => name.toLowerCase()),
      ...names(item.submenu ?? []),
    ]);
  }
  const top = (template) => template.map((item) => (item.role ?? item.label).toLowerCase());

  it("gives File (Ctrl+Q), Edit, View, Window and Help (About), and no developer tools when packaged", () => {
    const template = menuTemplate(true);
    assert.deepEqual(top(template), ["filemenu", "editmenu", "view", "windowmenu", "help"]);
    assert.ok(names(template).includes("about"));
    assert.ok(!names(template).includes("toggledevtools"));
    assert.ok(names(template).includes("reload"));
    assert.ok(["zoomin", "zoomout", "resetzoom"].every((role) => names(template).includes(role)));
  });

  it("offers the developer tools in a development run", () => {
    const template = menuTemplate(false);
    assert.deepEqual(top(template), ["filemenu", "editmenu", "view", "windowmenu", "help"]);
    assert.ok(names(template).includes("toggledevtools"));
  });

  it("hides the developer tools unless it is told the app is not packaged", () => {
    for (const packaged of [true, undefined, null, 1, "no", "false"]) {
      assert.ok(!names(menuTemplate(packaged)).includes("toggledevtools"), String(packaged));
    }
  });

  it("is made of roles and labelled submenus only, so no item can run code of its own", () => {
    const walk = (items) => {
      for (const item of items) {
        assert.ok(item.role || item.type === "separator" || (item.label && item.submenu), JSON.stringify(item));
        assert.equal(item.click, undefined, JSON.stringify(item));
        walk(item.submenu ?? []);
      }
    };
    for (const packaged of [true, false]) walk(menuTemplate(packaged));
  });
});

describe("buildChildEnv", () => {
  const parent = {
    PATH: "/usr/bin:/bin",
    HOME: "/home/tester",
    USER: "tester",
    LANG: "en_US.UTF-8",
    LC_ALL: "en_US.UTF-8",
    LC_CTYPE: "en_US.UTF-8",
    TMPDIR: "/var/tmp",
    // none of these may cross
    DISCORD_TOKEN: "parent-discord",
    ANTHROPIC_API_KEY: "parent-anthropic",
    GITHUB_TOKEN: "parent-github",
    AIHUB_OPENROUTER_KEY: "parent-openrouter",
    AIHUB_LAUNCH_TOKEN: "parent-token",
    AIHUB_PORT: "1",
    AIHUB_WEB_CONFIG: "/parent/web.yaml",
    PYTHONPATH: "/parent/python",
    PYTHONHOME: "/parent/home",
    LD_PRELOAD: "/parent/evil.so",
    https_proxy: "http://127.0.0.1:9",
  };
  const base = { parent, userData: "/data/aihub", port: 0, token: "tok", keys: {} };

  it("passes the seven named variables, the fixed settings, and nothing else", () => {
    assert.deepEqual(buildChildEnv(base), {
      PATH: "/usr/bin:/bin",
      HOME: "/home/tester",
      USER: "tester",
      LANG: "en_US.UTF-8",
      LC_ALL: "en_US.UTF-8",
      LC_CTYPE: "en_US.UTF-8",
      TMPDIR: "/var/tmp",
      AIHUB_WEB_CONFIG: "/data/aihub/web.yaml",
      AIHUB_STATE_DIR: "/data/aihub/state",
      AIHUB_PORT: "0",
      AIHUB_EXIT_ON_STDIN_EOF: "1",
      AIHUB_LAUNCH_TOKEN: "tok",
    });
  });

  it("leaves out a named variable that the parent does not have or has empty", () => {
    const env = buildChildEnv({ ...base, parent: { PATH: "/usr/bin:/bin", LANG: "" } });
    assert.equal(env.PATH, "/usr/bin:/bin");
    for (const name of ["HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"]) assert.ok(!(name in env), name);
  });

  it("takes the two keys from the keys file and no other name from it", () => {
    const keys = {
      AIHUB_OPENROUTER_KEY: "sk-or-1",
      AIHUB_ANTHROPIC_KEY: "sk-ant-2",
      DISCORD_TOKEN: "file-discord",
      AIHUB_LAUNCH_TOKEN: "file-token",
      AIHUB_PORT: "2",
      PATH: "/file/path",
      PYTHONPATH: "/file/python",
    };
    const env = buildChildEnv({ ...base, keys });
    assert.equal(env.AIHUB_OPENROUTER_KEY, "sk-or-1");
    assert.equal(env.AIHUB_ANTHROPIC_KEY, "sk-ant-2");
    assert.equal(env.AIHUB_LAUNCH_TOKEN, "tok");
    assert.equal(env.AIHUB_PORT, "0");
    assert.equal(env.PATH, "/usr/bin:/bin");
    assert.ok(!("DISCORD_TOKEN" in env));
    assert.ok(!("PYTHONPATH" in env));
  });

  it("never takes a key from the parent environment", () => {
    const env = buildChildEnv({ ...base, keys: {} });
    assert.ok(!("AIHUB_OPENROUTER_KEY" in env));
    assert.ok(!("AIHUB_ANTHROPIC_KEY" in env));
  });

  it("leaves out a key that is blank or not a string", () => {
    const env = buildChildEnv({ ...base, keys: { AIHUB_OPENROUTER_KEY: "  ", AIHUB_ANTHROPIC_KEY: 7 } });
    assert.ok(!("AIHUB_OPENROUTER_KEY" in env));
    assert.ok(!("AIHUB_ANTHROPIC_KEY" in env));
  });

  it("writes the port as text, and falls back to 0 for anything that is not a port", () => {
    assert.equal(buildChildEnv({ ...base, port: 41234 }).AIHUB_PORT, "41234");
    assert.equal(buildChildEnv({ ...base, port: 65535 }).AIHUB_PORT, "65535");
    for (const port of [undefined, null, NaN, -1, 65536, 1.5, "8787", "abc", {}, Infinity]) {
      assert.equal(buildChildEnv({ ...base, port }).AIHUB_PORT, "0", String(port));
    }
  });

  it("adds PYTHONPATH only when it is given one", () => {
    assert.ok(!("PYTHONPATH" in buildChildEnv(base)));
    assert.equal(buildChildEnv({ ...base, pythonPath: "/repo/app" }).PYTHONPATH, "/repo/app");
  });

  it("only returns strings", () => {
    const env = buildChildEnv({ ...base, keys: { AIHUB_OPENROUTER_KEY: "k" }, port: 7, pythonPath: "/repo/app" });
    for (const [name, value] of Object.entries(env)) assert.equal(typeof value, "string", name);
  });

  it("copes with a parent or a keys object that is missing, odd or inherits names", () => {
    for (const odd of [undefined, null, 0, "text", [], () => {}]) {
      assert.doesNotThrow(() => buildChildEnv({ ...base, parent: odd }));
      assert.doesNotThrow(() => buildChildEnv({ ...base, keys: odd }));
    }
    const inherited = Object.create({ PATH: "/inherited", AIHUB_OPENROUTER_KEY: "inherited" });
    const env = buildChildEnv({ ...base, parent: inherited, keys: inherited });
    assert.ok(!("PATH" in env));
    assert.ok(!("AIHUB_OPENROUTER_KEY" in env));
  });

  it("refuses to build an environment without a launch token or a data directory", () => {
    for (const token of [undefined, null, "", 5, {}]) {
      assert.throws(() => buildChildEnv({ ...base, token }), TypeError, `token ${String(token)}`);
    }
    for (const userData of [undefined, null, "", 5, {}]) {
      assert.throws(() => buildChildEnv({ ...base, userData }), TypeError, `userData ${String(userData)}`);
    }
  });
});
