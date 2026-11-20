// The Electron main process: one window on the aihub backend, which runs as a child process.
//
// What the window may load, open or ask for is decided in policy.js, the backend's life in backend.js, what the
// window shows of it (the page, the cookie, the dialog) in lifecycle.js, and what its session refuses in session.js.
// This file wires them to Electron.
// There is no preload script and no IPC: the page talks to the backend over HTTP, as in a browser.

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { app, BrowserWindow, clipboard, dialog, Menu, nativeImage, nativeTheme, session, shell, Tray } from "electron";
import { Backend } from "./backend.js";
import { Lifecycle } from "./lifecycle.js";
import { externalTarget, HOST_RESOLVER_RULES, isBackendUrl, menuTemplate } from "./policy.js";
import { lockDownSession } from "./session.js";

const SRC = fileURLToPath(new URL(".", import.meta.url));
// The colour the window opens with and the page has behind its first paint (theme-color in the app's index.html).
const PAPER = { light: "#f8fafd", dark: "#0c151d" };
// Same mark as the web chat's favicon (app/aihub_web/static/icon.svg), rasterized for the window and the tray.
const ICON = join(SRC, "assets", "icon-512.png");
const TRAY_ICON = join(SRC, "assets", "icon-32.png");

const { desktopName, version, aihubCommit } = JSON.parse(
  readFileSync(join(app.getAppPath(), "package.json"), "utf8"),
);
// Must be called before `ready`, and must match the installed .desktop file's name (see README, "Installing it and
// a menu entry") or the window manager cannot tell this window apart from an app with no icon of its own: it falls
// back to a generic, ungrouped entry with no custom icon, which is what sent us looking for this in the first place.
app.setDesktopName(desktopName);

// A development run keeps its own data directory (and with it its own single-instance lock and port).
app.setPath("userData", join(app.getPath("appData"), app.isPackaged ? "aihub-desktop" : "aihub-desktop-dev"));

// Render in software. On this machine (NVIDIA driver 615.78.08, KDE Wayland) Chromium's GPU process dies with SIGSEGV
// at start whenever the driver refuses it a GPU channel group (the kernel log shows NV_ERR_STATE_IN_USE just before).
// That comes and goes: the same build crashed on some launches and not on others. Chromium starts the GPU process
// three times before it gives up, and each crash leaves a 4 to 5 MB core dump on /. No switch that keeps GPU rendering
// avoided it (--disable-gpu-compositing, ANGLE on EGL, the ozone flags and --disable-gpu-sandbox were tried). A text
// chat does not need the GPU, and with this switch the app keeps no /dev/nvidia* file open and nvidia-smi lists no GPU
// memory for it (it still opens the render node /dev/dri/renderD128), which local image generation can use instead.
// It goes around the driver's refusal; it does not remove it.
app.commandLine.appendSwitch("disable-gpu");

// No host name resolves except the backend's address (see policy.js): a link the pointer only touches is not looked up.
app.commandLine.appendSwitch("host-resolver-rules", HOST_RESOLVER_RULES);

let backend = null;
let lifecycle = null;
let win = null;
let tray = null;
let backendStopped = false;
// Set as soon as a real quit begins (the tray's Quit item, the OS ending the session, app.quit() from anywhere), so
// the window's "close" handler below can tell a real quit from the user just clicking the window's close button.
let isQuitting = false;

// The origin of the backend the window may talk to, or null while there is none.
const currentOrigin = () => lifecycle?.origin ?? null;

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", showWindow);
  // The window close button hides to the tray instead (see createWindow), so this now only fires if something
  // destroys the window outright; a backend without a window has no use.
  app.on("window-all-closed", () => app.quit());
  app.on("before-quit", stopBackendThenQuit);
  app.whenReady().then(main).catch(fatal);
}

async function main() {
  const ses = session.defaultSession;
  lockDownSession(ses, { allows: (url) => lifecycle?.allows(url) === true });

  app.setAboutPanelOptions({
    applicationName: "aihub",
    applicationVersion: `${version} (${aihubCommit || "dev"})`,
    copyright: `Electron ${process.versions.electron}, Chromium ${process.versions.chrome}`,
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate(menuTemplate(app.isPackaged)));
  tray = createTray();

  backend = new Backend({
    userData: app.getPath("userData"),
    isPackaged: app.isPackaged,
    resourcesPath: process.resourcesPath,
  });
  win = createWindow();
  lifecycle = new Lifecycle({
    backend,
    window: win,
    cookies: ses.cookies,
    showDialog,
    quit: () => app.quit(),
    fail: fatal,
    loadingFile: join(SRC, "loading.html"),
  });
  void lifecycle.start();
}

/** A last resort for an error nothing else handles: say so, then leave. */
function fatal(error) {
  dialog.showErrorBox("aihub", `aihub could not start.\n\n${error?.stack ?? error}`);
  app.exit(1);
}

function createWindow() {
  const window = new BrowserWindow({
    title: "aihub",
    icon: ICON,
    width: 1180,
    height: 800,
    minWidth: 360,
    minHeight: 480,
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
  lockDown(window.webContents);
  // The close button leaves the app running in the tray, as Claude desktop and Antigravity do; only the tray's
  // Quit item, the menu's Quit, or the OS ending the session goes through to a real close.
  window.on("close", (event) => {
    if (isQuitting) return;
    event.preventDefault();
    window.hide();
  });
  return window;
}

/** The tray icon: click to show the window, or quit from its menu. Kept at module scope so it is never garbage
 * collected out from under its own icon. */
function createTray() {
  const t = new Tray(nativeImage.createFromPath(TRAY_ICON));
  t.setToolTip("aihub");
  t.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Show aihub", click: showWindow },
      { type: "separator" },
      { label: "Quit", click: () => { isQuitting = true; app.quit(); } },
    ]),
  );
  t.on("click", showWindow);
  return t;
}

/** Keeps the window on the backend: nothing opens in it, or beside it, except the backend's own pages. */
function lockDown(contents) {
  contents.setWindowOpenHandler(({ url }) => {
    const target = externalTarget(url, currentOrigin());
    // Looked up when used, not when this file loads, so that a test can stub it.
    if (target !== null) Promise.resolve(shell.openExternal(target)).catch(() => {});
    return { action: "deny" };
  });
  for (const event of ["will-navigate", "will-redirect", "will-frame-navigate"]) {
    contents.on(event, (details) => {
      if (!isBackendUrl(details.url, currentOrigin())) details.preventDefault();
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
  isQuitting = true;
  lifecycle?.beginQuit();
  if (backendStopped || backend === null) return;
  event.preventDefault();
  // The user has asked to leave; a stop can take a few seconds, and the window need not wait for it.
  for (const window of BrowserWindow.getAllWindows()) window.hide();
  backend.stop().then(() => {
    backendStopped = true;
    app.quit();
  });
}
