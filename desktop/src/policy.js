// What the main process allows, as pure functions with no Electron import, so node:test can check them.

import { join } from "node:path";

const WEB_PROTOCOLS = new Set(["http:", "https:"]);

// The only variables the backend inherits from the app that starts it, and only when they are set.
const INHERITED = ["PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR"];

// The only names taken from the keys file. The same file holds the Discord token, which must stay out.
const KEY_NAMES = ["AIHUB_OPENROUTER_KEY", "AIHUB_ANTHROPIC_KEY"];

/**
 * Chromium's host-resolver-rules switch: every host name fails to resolve at once, except the backend's own address.
 *
 * The page can show links to anywhere, and Chromium looks up the host of a link as soon as the pointer is over it or
 * it is clicked. That lookup is not a request, so the guard on requests never sees it, and it would tell a name server
 * which site the page links to. The backend is reached by an address that needs no lookup, and a link to the outside
 * opens in the system browser, which does its own.
 */
export const HOST_RESOLVER_RULES = "MAP * ~NOTFOUND , EXCLUDE 127.0.0.1";

/**
 * The request headers with the connection set to close after the request, whatever they said about it before.
 *
 * Chromium sends a request again, by itself and on a new connection, when the kept-alive connection it was first sent
 * on is closed before any answer comes. After a backend has died that is a way for a request to reach a program that
 * has bound the freed port, and no request hook sees it. A connection that is closed after every request is never
 * reused, so there is nothing to send again on.
 */
export function withoutKeepAlive(headers) {
  const plain = typeof headers === "object" && headers !== null && !Array.isArray(headers) ? headers : {};
  const kept = Object.entries(plain).filter(([name]) => name.toLowerCase() !== "connection");
  return { ...Object.fromEntries(kept), Connection: "close" };
}

/** The parsed URL, or null for anything that is not a string holding a URL. */
function parseUrl(value) {
  if (typeof value !== "string") return null;
  try {
    return new URL(value);
  } catch {
    return null;
  }
}

/** Whether the object has the property itself, and so did not get it from a prototype. */
const has = (object, name) => object != null && Object.hasOwn(object, name);

const isNonEmptyString = (value) => typeof value === "string" && value !== "";

/**
 * Whether `url` is a page of the backend, whose origin is `origin` (like "http://127.0.0.1:41234").
 *
 * Only http and https count. Other schemes have no origin of their own to compare ("null"), or borrow
 * the backend's (blob:http://127.0.0.1:41234/...). `origin` must be written exactly as new URL() writes
 * it, so a caller that gets it wrong refuses every page instead of guessing.
 */
export function isBackendUrl(url, origin) {
  const target = parseUrl(url);
  return target !== null && WEB_PROTOCOLS.has(target.protocol) && target.origin === origin;
}

/**
 * Whether a network request for `url` may go ahead while the backend's origin is `origin`.
 *
 * Only the backend itself may be asked for anything. This guards every request the window makes, so it is
 * stricter than isBackendUrl: besides naming the backend's origin, the URL must be spelled the way Chromium
 * spells the requests it reports (lower-case scheme and host, no user name, no zero in front of the port, a
 * slash after the origin). Any other spelling is refused, and so is every WebSocket. With an `origin` that is
 * not exactly an origin, which includes null and undefined, everything is refused: that is the state before the
 * first backend is up and from the moment it has ended.
 */
export function allowsRequest(url, origin) {
  return isBackendUrl(url, origin) && (url === origin || url.startsWith(`${origin}/`));
}

/**
 * The address to hand to the operating system for a link the window may not show, or null to do nothing.
 *
 * Web links go to the default browser, except links to the backend itself: opened there they would
 * arrive without the launch cookie. A mailto: link goes to the mail program only without a query,
 * which could prefill a body, a recipient list or an attachment. The result is the parser's own
 * spelling, so a tab or newline hidden in the text never reaches the operating system.
 */
export function externalTarget(url, origin) {
  const target = parseUrl(url);
  if (target === null) return null;
  if (WEB_PROTOCOLS.has(target.protocol)) return target.origin === origin ? null : target.href;
  if (target.protocol === "mailto:" && target.search === "") return target.href;
  return null;
}

/**
 * The application menu as an Electron template of roles, which Electron fills in.
 *
 * File carries Quit (Ctrl+Q) and Help carries About. The developer tools are offered only when
 * `isPackaged` is false; anything else, including a missing value, hides them.
 */
export function menuTemplate(isPackaged) {
  const view = {
    label: "View",
    submenu: [
      { role: "reload" },
      { role: "forceReload" },
      ...(isPackaged === false ? [{ role: "toggleDevTools" }] : []),
      { type: "separator" },
      { role: "resetZoom" },
      { role: "zoomIn" },
      { role: "zoomOut" },
      { type: "separator" },
      { role: "togglefullscreen" },
    ],
  };
  return [
    { role: "fileMenu" },
    { role: "editMenu" },
    view,
    { role: "windowMenu" },
    { role: "help", submenu: [{ role: "about" }] },
  ];
}

/**
 * The whole environment of the backend process, built from nothing.
 *
 * - parent: the environment of the app, from which only the INHERITED names are read.
 * - keys: the parsed keys file, from which only the two key names are read, and only when not blank.
 * - pythonPath: where the aihub_web package is when the interpreter runs without -I (development).
 *
 * `parent` and `keys` come from outside and may hold anything. `userData` and `token` are the app's
 * own: a missing token would start a server that any program on the machine can use, so that throws.
 */
export function buildChildEnv({ parent, userData, port, token, keys, pythonPath }) {
  if (!isNonEmptyString(userData)) throw new TypeError("buildChildEnv needs the data directory");
  if (!isNonEmptyString(token)) throw new TypeError("buildChildEnv needs a launch token");
  const env = {};
  for (const name of INHERITED) {
    if (has(parent, name) && isNonEmptyString(parent[name])) env[name] = parent[name];
  }
  env.AIHUB_WEB_CONFIG = join(userData, "web.yaml");
  env.AIHUB_STATE_DIR = join(userData, "state");
  // 0 asks the system for a free port.
  env.AIHUB_PORT = String(Number.isInteger(port) && port >= 0 && port <= 65535 ? port : 0);
  env.AIHUB_EXIT_ON_STDIN_EOF = "1";
  env.AIHUB_LAUNCH_TOKEN = token;
  for (const name of KEY_NAMES) {
    if (has(keys, name) && typeof keys[name] === "string" && keys[name].trim() !== "") env[name] = keys[name];
  }
  if (isNonEmptyString(pythonPath)) env.PYTHONPATH = pythonPath;
  return env;
}
