// What the window holds for the backend, and what happens to it when a backend starts, ends or is started again:
// the page, the cookie and the origin that go with each backend, and the dialog that follows when one ends.
//
// Nothing here imports Electron. main.js hands in the window, the cookie jar, the dialog and the way out, so that
// node:test can drive all of it with stand-ins.

import { fileURLToPath } from "node:url";
import { allowsRequest } from "./policy.js";

/** The cookie that carries the launch token the backend demands on every /api/ request. */
export const COOKIE = "aihub_t";

// How many times in a row the loading page is asked for again after something else has settled in its place.
const LOADING_REPEATS = 5;

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
  #repeats = 0;

  /**
   * - backend: the Backend: start(), stop(), isAlive(), origin, logPath and an "exit" event.
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

  /**
   * The origin of the backend that is up, like "http://127.0.0.1:41234", or null: none has started yet, or the
   * process of the last one is gone, even if its exit event has not come in.
   */
  get origin() {
    return this.#origin !== null && this.#backend.isAlive() ? this.#origin : null;
  }

  /**
   * Whether the window may ask for `url`: it must be a page of the backend that is up, and that backend must still
   * run. The exit event comes some milliseconds after the death, and a request in between would reach whatever
   * binds the freed port.
   */
  allows(url) {
    const origin = this.origin;
    return origin !== null && allowsRequest(url, origin);
  }

  /** From here on the app is leaving: a backend that ends is expected to, and no dialog follows. */
  beginQuit() {
    this.#quitting = true;
  }

  /** Shows the first backend. What goes wrong ends in a dialog, or in `fail`; the promise never rejects. */
  start() {
    this.#backend.on("exit", (event) => this.#ended(event));
    this.#window.webContents.on("did-stop-loading", () => this.#keepLoadingPage());
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
    this.#startLoadingPage();
    if (gone !== null) await this.#cookies.remove(gone, COOKIE);
  }

  /** The loading page as the start of an episode (a backend has ended, or one is about to start), with a full budget. */
  #startLoadingPage() {
    this.#repeats = 0;
    this.#showLoadingPage();
  }

  #showLoadingPage() {
    if (!this.#window.isDestroyed()) this.#window.loadFile(this.#loadingFile).catch(() => {});
  }

  /**
   * While there is no backend the window belongs on the loading page, and whatever settles in it is looked at. A
   * navigation that began just before the backend ended (a reload) can finish after the loading page has loaded: on
   * the old page, or on the error page Chromium shows for the request the guard cancelled.
   */
  #keepLoadingPage() {
    if (this.#origin !== null || this.#quitting || this.#window.isDestroyed()) return;
    if (this.#showsLoadingPage()) {
      this.#repeats = 0;
    } else if (this.#repeats < LOADING_REPEATS) {
      this.#repeats++;
      this.#showLoadingPage();
    }
  }

  #showsLoadingPage() {
    try {
      return fileURLToPath(this.#window.webContents.getURL()) === this.#loadingFile;
    } catch {
      return false; // not a file: address at all
    }
  }

  /**
   * Shows the loading page, starts the backend, gives the window the cookie for the token that backend was
   * started with, and loads its page. A restart goes through here as well, and loads the origin of the new
   * backend: the port may have changed, and reloading the old URL could reach whatever has taken that port since.
   *
   * The origin is set last, once the cookie is in place, so a start that fails leaves it null.
   */
  async #show() {
    this.#startLoadingPage();
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
          // A page that failed to load leaves its backend running, and start() refuses while one runs. When none
          // runs (it died, or never started) there is nothing to stop. The exit of the one that is stopped here
          // reaches #ended, which takes its origin and cookie and finds the dialog still open.
          await this.#backend.stop();
          if (this.#quitting) return;
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
