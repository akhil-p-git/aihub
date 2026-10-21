// What the window holds for the backend, and what happens to it when a backend starts, ends or is started again:
// the page, the cookie and the origin that go with each backend, and the dialog that follows when one ends.
//
// Nothing here imports Electron. main.js hands in the window, the cookie jar, the dialog and the way out, so that
// node:test can drive all of it with stand-ins.

import { allowsRequest } from "./policy.js";

/** The cookie that carries the launch token the backend demands on every /api/ request. */
export const COOKIE = "aihub_t";

export class Lifecycle {
  #backend;
  #window;
  #cookies;
  #showDialog;
  #quit;
  #fail;
  #loadingFile;
  // The origin of the backend the window may talk to. It is null before the first backend is up and from the moment
  // one has ended, and while it is null the window can reach nothing.
  #origin = null;
  #quitting = false;
  #offering = false;

  /**
   * - backend: the Backend: start(), origin, logPath and an "exit" event.
   * - window: the BrowserWindow that shows the loading page and then the backend's page.
   * - cookies: the cookie jar of the window's session: set(cookie) and remove(url, name).
   * - showDialog(options): the message box; resolves with { response }.
   * - quit(): leaves the app. fail(error): the last resort for an error nothing else handles.
   * - loadingFile: the path of the loading page.
   */
  constructor({ backend, window, cookies, showDialog, quit, fail, loadingFile }) {
    this.#backend = backend;
    this.#window = window;
    this.#cookies = cookies;
    this.#showDialog = showDialog;
    this.#quit = quit;
    this.#fail = fail;
    this.#loadingFile = loadingFile;
  }

  /** The origin of the backend that is up, like "http://127.0.0.1:41234", or null. */
  get origin() {
    return this.#origin;
  }

  /** Whether the window may ask for `url`: it must be a page of the backend that is up. */
  allows(url) {
    return this.#origin !== null && allowsRequest(url, this.#origin);
  }

  /** From here on the app is leaving: a backend that ends is expected to, and no dialog follows. */
  beginQuit() {
    this.#quitting = true;
  }

  /** Shows the first backend. What goes wrong ends in a dialog, or in `fail`; the promise never rejects. */
  start() {
    this.#backend.on("exit", (event) => this.#ended(event));
    return this.#show()
      .catch((error) => {
        if (!this.#quitting) return this.#offerRestart("aihub could not start its backend.", error.message);
      })
      .catch(this.#fail);
  }

  /** The backend has ended, however it ended. */
  #ended({ code, signal }) {
    // The port is free from now on, and whatever binds it next must get nothing from this window. Nulling the
    // origin stops every request at once; the cookie and the page follow before anybody is asked anything.
    const gone = this.#origin;
    this.#origin = null;
    if (this.#quitting) return;
    const how = signal ? `signal ${signal}` : `exit code ${code}`;
    this.#forget(gone)
      .then(() => this.#offerRestart(`The aihub backend stopped (${how}).`, ""))
      .catch(this.#fail);
  }

  /** Takes what the window held for the backend at `gone` (null if it held nothing): the page, and the cookie. */
  async #forget(gone) {
    this.#showLoadingPage();
    if (gone !== null) await this.#cookies.remove(gone, COOKIE);
  }

  #showLoadingPage() {
    if (!this.#window.isDestroyed()) this.#window.loadFile(this.#loadingFile).catch(() => {});
  }

  /**
   * Shows the loading page, starts the backend, gives the window the cookie for the token that backend was
   * started with, and loads its page. A restart goes through here as well, and loads the origin of the new
   * backend: the port may have changed, and reloading the old URL could reach whatever has taken that port since.
   *
   * The origin is set last, once the cookie is in place, so a start that fails leaves it null.
   */
  async #show() {
    this.#showLoadingPage();
    const started = await this.#backend.start();
    if (this.#quitting) return;
    const stillUp = () => this.#backend.origin === started.origin;
    await this.#cookies.set({
      url: started.origin,
      name: COOKIE,
      value: started.token,
      httpOnly: true,
      sameSite: "strict",
      // no expiry: the cookie ends with the session
    });
    if (!stillUp()) {
      // It ended while the cookie was being set, before the exit handler had an origin to forget.
      await this.#cookies.remove(started.origin, COOKIE);
      throw new Error("The aihub backend stopped as soon as it was up.");
    }
    this.#origin = started.origin;
    try {
      await this.#window.loadURL(this.#origin);
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
  async #offerRestart(message, detail) {
    if (this.#offering) return;
    this.#offering = true;
    try {
      for (;;) {
        const { response } = await this.#showDialog({
          type: "error",
          title: "aihub",
          message,
          detail: [detail, `Log: ${this.#backend.logPath}`].filter(Boolean).join("\n\n"),
          buttons: ["Restart", "Quit"],
          defaultId: 0,
          cancelId: 1,
          noLink: true,
        });
        if (response !== 0 || this.#quitting) {
          this.#quit();
          return;
        }
        try {
          await this.#show();
          return;
        } catch (error) {
          if (this.#quitting) return;
          message = "aihub could not start its backend.";
          detail = error.message;
        }
      }
    } finally {
      this.#offering = false;
    }
  }
}
