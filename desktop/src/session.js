// What the window's session is told to refuse or to change: requests, permissions and one download.
//
// Nothing here imports Electron. main.js passes the session, so that node:test can hand in a stand-in that keeps what
// it was asked to register, and the wiring the security of the window rests on does not go unchecked.

import { withoutKeepAlive } from "./policy.js";

// The requests the guard looks at, which are the ones that reach the network. The loading page (file:), devtools:
// and data: URLs are not among them.
export const GUARDED_URLS = ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"];
// The requests whose connection is closed after them. A WebSocket handshake has to keep its Upgrade.
export const HTTP_URLS = ["http://*/*", "https://*/*"];
// The only thing the page may ask for: the copy buttons of code blocks write to the clipboard.
export const ALLOWED_PERMISSIONS = new Set(["clipboard-sanitized-write"]);

/**
 * Locks down `ses`, the session of the window. `allows(url)` says whether a request may go out; it is asked for every
 * request, so that its answer can change while the app runs.
 */
export function lockDownSession(ses, { allows }) {
  // Chromium's only request off this machine is a spellcheck dictionary download; spellcheck is off anyway.
  ses.setSpellCheckerDictionaryDownloadURL("http://127.0.0.1:9/");
  ses.setPermissionRequestHandler((_contents, permission, callback) => callback(ALLOWED_PERMISSIONS.has(permission)));
  ses.setPermissionCheckHandler((_contents, permission) => ALLOWED_PERMISSIONS.has(permission));
  // Only the backend that is up now may be asked for anything, and each hop of a redirect is a request like any
  // other. A page that outlives its backend therefore cannot hand its cookie to whatever binds the port next.
  ses.webRequest.onBeforeRequest({ urls: GUARDED_URLS }, (details, callback) => {
    callback({ cancel: allows(details.url) !== true });
  });
  // Every connection ends with its request, so that Chromium never has one to send a request again on (see policy.js).
  ses.webRequest.onBeforeSendHeaders({ urls: HTTP_URLS }, (details, callback) => {
    callback({ requestHeaders: withoutKeepAlive(details.requestHeaders) });
  });
}
