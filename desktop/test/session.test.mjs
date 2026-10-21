// Run inside desktop/ with: npm test
//
// lockDownSession() is given a stand-in for Electron's session that keeps what it was asked to register, so that
// what main.js depends on can be checked without a window: which requests are looked at, which are cancelled, what
// the page may ask permission for, and that every request asks for its connection to be closed.
import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { ALLOWED_PERMISSIONS, GUARDED_URLS, HTTP_URLS, lockDownSession } from "../src/session.js";

const ORIGIN = "http://127.0.0.1:4001";

function fakeSession() {
  const registered = {};
  return {
    registered,
    setSpellCheckerDictionaryDownloadURL: (url) => (registered.dictionaryUrl = url),
    setPermissionRequestHandler: (handler) => (registered.permissionRequest = handler),
    setPermissionCheckHandler: (handler) => (registered.permissionCheck = handler),
    webRequest: {
      onBeforeRequest: (filter, listener) => (registered.beforeRequest = { filter, listener }),
      onBeforeSendHeaders: (filter, listener) => (registered.beforeSendHeaders = { filter, listener }),
    },
  };
}

/** A session locked down with an `allows` that lets the pages of ORIGIN through. */
function locked(allows = (url) => url === ORIGIN || url.startsWith(`${ORIGIN}/`)) {
  const ses = fakeSession();
  lockDownSession(ses, { allows });
  return ses.registered;
}

/** What a listener of the session answers to a request, through the callback it is given. */
const answer = (listener, details) => {
  let result;
  listener(details, (value) => (result = value));
  return result;
};

describe("the request guard", () => {
  it("looks at http, https, ws and wss requests, which are the ones that reach the network", () => {
    assert.deepEqual(locked().beforeRequest.filter, { urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"] });
    assert.deepEqual(GUARDED_URLS, ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"]);
  });

  it("lets through the requests `allows` accepts and cancels every other one", () => {
    const { listener } = locked().beforeRequest;
    assert.deepEqual(answer(listener, { url: `${ORIGIN}/api/chat` }), { cancel: false });
    assert.deepEqual(answer(listener, { url: `${ORIGIN}/` }), { cancel: false });
    for (const url of [
      "http://127.0.0.1:4002/",
      "https://example.com/",
      "ws://127.0.0.1:4001/",
      "http://localhost:4001/",
    ]) {
      assert.deepEqual(answer(listener, { url }), { cancel: true }, url);
    }
  });

  it("cancels unless `allows` says exactly true", () => {
    for (const verdict of [false, undefined, null, 0, 1, "true", {}]) {
      const { listener } = locked(() => verdict).beforeRequest;
      assert.deepEqual(answer(listener, { url: `${ORIGIN}/` }), { cancel: true }, String(verdict));
    }
  });

  it("asks `allows` again for every request, so a restart on another port moves the allowance", () => {
    let origin = ORIGIN;
    const { listener } = locked((url) => url.startsWith(origin)).beforeRequest;
    assert.deepEqual(answer(listener, { url: `${ORIGIN}/a` }), { cancel: false });
    origin = "http://127.0.0.1:4002";
    assert.deepEqual(answer(listener, { url: `${ORIGIN}/a` }), { cancel: true });
    assert.deepEqual(answer(listener, { url: `${origin}/a` }), { cancel: false });
  });
});

describe("the connection of every request", () => {
  it("is asked to close, for http and https requests only", () => {
    const { filter } = locked().beforeSendHeaders;
    assert.deepEqual(filter, { urls: ["http://*/*", "https://*/*"] });
    assert.deepEqual(HTTP_URLS, ["http://*/*", "https://*/*"]);
    assert.ok(!filter.urls.some((pattern) => pattern.startsWith("ws")), "a WebSocket handshake needs its Upgrade");
  });

  it("replaces what the request said about it and keeps its other headers, the cookie included", () => {
    const { listener } = locked().beforeSendHeaders;
    const requestHeaders = { Connection: "keep-alive", Cookie: "aihub_t=fixture", Accept: "*/*" };
    assert.deepEqual(answer(listener, { url: `${ORIGIN}/api/chat`, requestHeaders }), {
      requestHeaders: { Cookie: "aihub_t=fixture", Accept: "*/*", Connection: "close" },
    });
  });
});

describe("what the page may ask permission for", () => {
  it("is the clipboard write of the copy buttons and nothing else", () => {
    assert.deepEqual([...ALLOWED_PERMISSIONS], ["clipboard-sanitized-write"]);
    const { permissionRequest, permissionCheck } = locked();
    for (const permission of ["clipboard-sanitized-write"]) {
      let granted;
      permissionRequest(null, permission, (value) => (granted = value));
      assert.equal(granted, true, permission);
      assert.equal(permissionCheck(null, permission), true, permission);
    }
    for (const permission of ["media", "geolocation", "notifications", "clipboard-read", "midi", "openExternal", ""]) {
      let granted;
      permissionRequest(null, permission, (value) => (granted = value));
      assert.equal(granted, false, permission);
      assert.equal(permissionCheck(null, permission), false, permission);
    }
  });
});

describe("the spellcheck dictionary", () => {
  it("is fetched from a closed loopback port, so that Chromium has no request of its own to make off this machine", () => {
    assert.equal(locked().dictionaryUrl, "http://127.0.0.1:9/");
  });
});
