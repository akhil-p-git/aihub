// The Electron main process: one window on the aihub backend, which runs as a child process.
//
// What the window may load, open or ask for is decided in policy.js, and the backend's life in backend.js.
// There is no preload script and no IPC: the page talks to the backend over HTTP, as in a browser.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { app, BrowserWindow, clipboard, dialog, Menu, nativeTheme, session, shell } from "electron";
import { Backend } from "./backend.js";
import { allowsRequest, externalTarget, isBackendUrl, menuTemplate } from "./policy.js";

const SRC = fileURLToPath(new URL(".", import.meta.url));
const COOKIE = "aihub_t";
// The requests the guard looks at, which are the ones that reach the network. The loading page (file:), devtools:
// and data: URLs are not among them.
const GUARDED_URLS = ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"];
// The only thing the page may ask for: the copy buttons of code blocks write to the clipboard.
const ALLOWED_PERMISSIONS = new Set(["clipboard-sanitized-write"]);
// The colour of the page behind its first paint (theme-color in the app's index.html).
const PAPER = { light: "#f8fafd", dark: "#0c151d" };

// A development run keeps its own data directory (and with it its own single-instance lock and port).
app.setPath("userData", join(app.getPath("appData"), app.isPackaged ? "aihub-desktop" : "aihub-desktop-dev"));

// Render in software. On this machine (NVIDIA driver 615.78.08 on KDE Wayland) Chromium's GPU process dies with
// SIGSEGV on a NULL pointer, in libnvidia-eglcore or in Chromium just after it, three times at every start, whenever
// the driver refuses it a GPU channel group (the kernel log shows NV_ERR_STATE_IN_USE just before). Each crash leaves
// a 4 to 5 MB core dump on /, and Chromium gives up on the GPU after the third anyway. Software rendering is enough
// for a text chat, and the app then holds no GPU memory or channels, which local image generation needs.
app.commandLine.appendSwitch("disable-gpu");

let backend = null;
let win = null;
// The origin of the backend the window may talk to. It is null before the first backend is up and from the moment
// one has ended, and while it is null the window can reach nothing.
let origin = null;
let quitting = false;
let backendStopped = false;
let offering = false;

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showWindow);
  // Closing the last window quits: a backend without a window has no use.
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", stopBackendThenQuit);
  app.whenReady().then(main).catch(fatal);
}

async function main() {
  const ses = session.defaultSession;
  // Chromium's only request off this machine is a spellcheck dictionary download; spellcheck is off anyway.
  ses.setSpellCheckerDictionaryDownloadURL("http://127.0.0.1:9/");
  ses.setPermissionRequestHandler((_contents, permission, callback) => callback(ALLOWED_PERMISSIONS.has(permission)));
  ses.setPermissionCheckHandler((_contents, permission) => ALLOWED_PERMISSIONS.has(permission));
  // Only the backend that is up now may be asked for anything, and each hop of a redirect is a request like any
  // other. A page that outlives its backend therefore cannot hand its cookie to whatever binds the port next.
  ses.webRequest.onBeforeRequest({ urls: GUARDED_URLS }, (details, callback) => {
    callback({ cancel: !allowsRequest(details.url, origin) });
  });

  const { version, aihubCommit } = JSON.parse(readFileSync(join(app.getAppPath(), "package.json"), "utf8"));
  app.setAboutPanelOptions({
    applicationName: "aihub",
    applicationVersion: `${version} (${aihubCommit || "dev"})`,
    copyright: `Electron ${process.versions.electron}, Chromium ${process.versions.chrome}`,
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate(menuTemplate(app.isPackaged)));

  backend = new Backend({
    userData: app.getPath("userData"),
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
  });
  backend.on("exit", ({ code, signal }) => {
    // The port is free from now on, and whatever binds it next must get nothing from this window. Nulling the
    // origin stops every request at once; the cookie and the page follow before anybody is asked anything.
    const gone = origin;
    origin = null;
    if (quitting) return;
    const how = signal ? `signal ${signal}` : `exit code ${code}`;
    forgetBackend(gone)
      .then(() => offerRestart(`The aihub backend stopped (${how}).`, ""))
      .catch(fatal);
  });

  win = createWindow();
  showBackend()
    .catch((error) => {
      if (!quitting) return offerRestart("aihub could not start its backend.", error.message);
    })
    .catch(fatal);
}

/** A last resort for an error nothing else handles: say so, then leave. */
function fatal(error) {
  dialog.showErrorBox("aihub", `aihub could not start.\n\n${error?.stack ?? error}`);
  app.exit(1);
}

function createWindow() {
  const window = new BrowserWindow({
    title: "aihub",
    width: 1180,
    height: 800,
    minWidth: 360,
    minHeight: 480,
    show: false, // shown once the loading page has painted, so the window never opens blank
    autoHideMenuBar: true,
    backgroundColor: nativeTheme.shouldUseDarkColors ? PAPER.dark : PAPER.light,
    webPreferences: {
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      spellcheck: false,
      devTools: !app.isPackaged,
    },
  });
  window.once("ready-to-show", () => window.show());
  lockDown(window.webContents);
  return window;
}

/** Keeps the window on the backend: nothing opens in it, or beside it, except the backend's own pages. */
function lockDown(contents) {
  contents.setWindowOpenHandler(({ url }) => {
    const target = externalTarget(url, origin);
    // Looked up when used, not when this file loads, so that a test can stub it.
    if (target !== null) Promise.resolve(shell.openExternal(target)).catch(() => {});
    return { action: "deny" };
  });
  for (const event of ["will-navigate", "will-redirect", "will-frame-navigate"]) {
    contents.on(event, (details) => {
      if (!isBackendUrl(details.url, origin)) details.preventDefault();
    });
  }
  contents.on("will-attach-webview", (event) => event.preventDefault());
  contents.on("context-menu", (_event, params) => contextMenu(params).popup());
}

/** Cut, Copy and Paste where text can be edited, Copy for a selection, Copy link on a link, and Select all. */
function contextMenu({ linkURL, isEditable, selectionText, editFlags }) {
  const items = [];
  if (linkURL) items.push({ label: "Copy link", click: () => clipboard.writeText(linkURL) }, { type: "separator" });
  if (isEditable) {
    items.push(
      { role: "cut", enabled: editFlags.canCut },
      { role: "copy", enabled: editFlags.canCopy },
      { role: "paste", enabled: editFlags.canPaste },
    );
  } else if (selectionText) {
    items.push({ role: "copy" });
  }
  items.push({ role: "selectAll" });
  return Menu.buildFromTemplate(items);
}

function showLoadingPage() {
  if (win && !win.isDestroyed()) win.loadFile(join(SRC, "loading.html")).catch(() => {});
}

/** Takes what the window held for the backend at `gone` (null if it held nothing): the page, and the cookie. */
async function forgetBackend(gone) {
  showLoadingPage();
  if (gone !== null) await session.defaultSession.cookies.remove(gone, COOKIE);
}

/**
 * Shows the loading page, starts the backend, gives the window the cookie for the token that backend was
 * started with, and loads its page. A restart goes through here as well, and loads the origin of the new
 * backend: the port may have changed, and reloading the old URL could reach whatever has taken that port since.
 *
 * The origin is set last, once the cookie is in place, so a start that fails leaves it null.
 */
async function showBackend() {
  showLoadingPage();
  const started = await backend.start();
  if (quitting) return;
  const stillUp = () => backend.origin === started.origin;
  await session.defaultSession.cookies.set({
    url: started.origin,
    name: COOKIE,
    value: started.token,
    httpOnly: true,
    sameSite: "strict",
    // no expiry: the cookie ends with the session
  });
  if (!stillUp()) {
    // It ended while the cookie was being set, before the exit handler had an origin to forget.
    await session.defaultSession.cookies.remove(started.origin, COOKIE);
    throw new Error("The aihub backend stopped as soon as it was up.");
  }
  origin = started.origin;
  try {
    await win.loadURL(origin);
  } catch (error) {
    // The exit handler's loading page interrupts a page that is still loading when the backend ends.
    if (!stillUp()) throw new Error("The aihub backend stopped while its page was loading.");
    throw error;
  }
}

/**
 * Tells what went wrong and, until the backend runs again or the user quits, offers to start it again.
 * The backend's log is named in the dialog.
 */
async function offerRestart(message, detail) {
  if (offering) return;
  offering = true;
  try {
    for (;;) {
      const { response } = await showDialog({
        type: "error",
        title: "aihub",
        message,
        detail: [detail, `Log: ${backend.logPath}`].filter(Boolean).join("\n\n"),
        buttons: ["Restart", "Quit"],
        defaultId: 0,
        cancelId: 1,
        noLink: true,
      });
      if (response !== 0 || quitting) {
        app.quit();
        return;
      }
      try {
        await showBackend();
        return;
      } catch (error) {
        if (quitting) return;
        message = "aihub could not start its backend.";
        detail = error.message;
      }
    }
  } finally {
    offering = false;
  }
}

function showDialog(options) {
  return win && !win.isDestroyed() ? dialog.showMessageBox(win, options) : dialog.showMessageBox(options);
}

function showWindow() {
  if (!win || win.isDestroyed()) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

/** Holds the quit until the backend has stopped, so that a reply that is streaming is stored as stopped. */
function stopBackendThenQuit(event) {
  quitting = true;
  if (backendStopped || backend === null) return;
  event.preventDefault();
  // The user has asked to leave; a stop can take a few seconds, and the window need not wait for it.
  for (const window of BrowserWindow.getAllWindows()) window.hide();
  backend.stop().then(() => {
    backendStopped = true;
    app.quit();
  });
}
