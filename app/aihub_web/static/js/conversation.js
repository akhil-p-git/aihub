// What a conversation looks like: your messages, and a reply that can stream, finish, stop or fail.

import { bars, h, icon, working } from "./dom.js";
import { barsFor, formatCost, formatDuration, formatSpeed, levelLabel } from "./format.js";
import { createRenderer } from "./markdown.js";

let hooks = {
  modelName: (id) => id,
  levelsFor: () => [],
  onRetry() {},
  onChoose() {},
  onCopy() {},
};

/** main.js supplies what a reply needs from the rest of the app. */
export function configureConversation(next) {
  hooks = { ...hooks, ...next };
}

export function userMessage(message) {
  return h("article", { class: "msg user", "data-id": message.id }, h("div", { class: "bubble" }, message.content));
}

const ERROR_TITLES = {
  rate_limit: "Rate limit reached",
  quota: "Out of credit or over the spend limit",
  auth: "The API key was rejected",
  unreachable: "Can't reach the model",
  overloaded: "The provider is busy",
  timeout: "The model stopped responding",
  model_not_found: "That model isn't available",
  routing: "No host fits the routing rules",
  bad_request: "The request was refused",
  internal: "Something broke inside aihub",
  interrupted: "The connection dropped",
};

function errorNotice(message) {
  const { code, message: text } = message.error ?? {};
  return h(
    "div",
    { class: "notice", role: "alert" },
    h("strong", {}, icon("alert"), ERROR_TITLES[code] ?? "The reply failed"),
    h("span", {}, text ?? "The reply did not finish."),
    h(
      "div",
      { class: "row" },
      h("button", { class: "btn", type: "button", onClick: () => hooks.onRetry() }, icon("retry"), "Retry"),
      h("button", { class: "btn", type: "button", onClick: () => hooks.onChoose() }, "Choose another model"),
    ),
  );
}

function meta(message, isLast) {
  const usage = message.usage ?? {};
  const item = (text, title) => h("span", { title }, text);
  const parts = [item(hooks.modelName(message.model))];
  if (message.effort) {
    parts.push(
      h(
        "span",
        { class: "lvl", title: `Intensity: ${levelLabel(message.effort)}` },
        bars(barsFor(message.effort, hooks.levelsFor(message.model))),
        levelLabel(message.effort),
      ),
    );
  }
  if (usage.tps) parts.push(item(formatSpeed(usage.tps), "Generation speed"));
  if (usage.out) parts.push(item(`${usage.out.toLocaleString()} tokens`, "Tokens in the reply, thinking included"));
  if (usage.total_ms) parts.push(item(formatDuration(usage.total_ms), "Total time"));
  if (usage.cost) parts.push(item(formatCost(usage.cost), "Estimated from token counts and the price list"));
  const actions =
    message.status === "error"
      ? null
      : h(
          "span",
          { class: "actions" },
          h(
            "button",
            { class: "btn", type: "button", "data-copy-reply": true, "aria-label": "Copy reply" },
            icon("copy"),
            h("span", {}, "Copy"),
          ),
          isLast
            ? h(
                "button",
                { class: "btn", type: "button", "aria-label": "Retry reply", onClick: () => hooks.onRetry() },
                icon("retry"),
                h("span", {}, "Retry"),
              )
            : null,
        );
  return h("div", { class: "meta" }, parts, actions);
}

const registry = new WeakMap();

/** The ReplyView that built a reply element, so the Copy button can reach its text. */
export const replyFor = (el) => registry.get(el);

export class ReplyView {
  /** `waiting` is the line shown until the first token; `onFrame` runs after each batch of updates. */
  constructor({ waiting = "Waiting for the model…", model = null, effort = null, onFrame = () => {} } = {}) {
    this.model = model;
    this.effort = effort;
    this.onFrame = onFrame;
    this.text = "";
    this.thinking = "";
    this.pendingThinking = "";
    this.drawn = "";
    this.raf = 0;
    this.timer = 0;
    this.thinkStart = 0;
    this.el = h("article", { class: "msg assistant" });
    registry.set(this.el, this);
    this.body = h("div", { class: "body" });
    this.wait = working(waiting);
    this.el.append(this.wait, this.body);
    this.render = createRenderer(this.body);
  }

  addThinking(delta) {
    if (!this.details) this.#openThinking();
    this.thinking += delta;
    this.pendingThinking += delta;
    this.#schedule();
  }

  addText(delta) {
    this.#stopTimer();
    this.wait.remove();
    this.text += delta;
    this.#schedule();
  }

  #openThinking() {
    this.wait.remove();
    this.label = h("span", {}, "Thinking…");
    this.thinkNode = document.createTextNode("");
    this.thinkText = h("div", { class: "think-text" }, this.thinkNode);
    this.details = h(
      "details",
      { class: "think" },
      h("summary", {}, h("span", { class: "working" }, bars(4), this.label)),
      this.thinkText,
    );
    this.el.insertBefore(this.details, this.body);
    this.thinkStart = performance.now();
    this.timer = setInterval(() => {
      const seconds = Math.round((performance.now() - this.thinkStart) / 1000);
      this.label.textContent = seconds ? `Thinking… ${seconds} s` : "Thinking…";
    }, 1000);
  }

  #stopTimer() {
    clearInterval(this.timer);
    this.timer = 0;
  }

  #schedule() {
    if (!this.raf) this.raf = requestAnimationFrame(() => this.#flush());
  }

  #flush() {
    this.raf = 0;
    if (this.pendingThinking) {
      this.thinkNode.appendData(this.pendingThinking);
      this.pendingThinking = "";
      this.thinkText.scrollTop = this.thinkText.scrollHeight;
    }
    if (this.text !== this.drawn) {
      this.drawn = this.text;
      this.render.update(this.text);
    }
    this.onFrame();
  }

  /** Ends the reply with the stored message (or a local stand-in when the stream was cut). */
  finish(message, { last = true } = {}) {
    cancelAnimationFrame(this.raf);
    this.raf = 0;
    this.#stopTimer();
    this.wait.remove();
    const text = message.content || this.text;
    if (message.thinking || this.thinking)
      this.#settleThinking(message.thinking || this.thinking, message.usage?.think_ms);
    this.render.update(text, true);
    this.drawn = this.text = text;
    this.copyText = text;
    this.el.dataset.id = message.id;
    this.el.dataset.status = message.status;
    if (message.status === "error") this.el.append(errorNotice(message));
    this.el.append(meta(message, last));
    if (message.status === "stopped") {
      this.el.append(h("p", { class: "notice-inline" }, "Stopped before it finished."));
    } else if (message.status === "done" && message.usage?.finish === "length") {
      this.el.append(
        h(
          "p",
          { class: "notice-inline" },
          "Cut off at the output limit. Retry with lower intensity for a shorter answer.",
        ),
      );
    }
    this.onFrame();
  }

  #settleThinking(thinking, ms) {
    if (!this.details) {
      this.thinkNode = document.createTextNode(thinking);
      this.thinkText = h("div", { class: "think-text" }, this.thinkNode);
      this.label = h("span", {});
      this.details = h("details", { class: "think" }, h("summary", {}, this.label), this.thinkText);
      this.el.insertBefore(this.details, this.body);
    } else {
      this.thinkNode.data = thinking;
      this.details.querySelector("summary").replaceChildren(this.label);
    }
    this.label.textContent = ms ? `Thought for ${formatDuration(ms)}` : "Reasoning";
  }

  /** The reply as the person last saw it, for the Copy button. */
  get copyable() {
    return this.copyText ?? this.text;
  }

  /** A reply loaded from storage: no streaming, rendered once. */
  static from(message, { last = false } = {}) {
    const view = new ReplyView({ model: message.model, effort: message.effort });
    view.finish(message, { last });
    return view;
  }
}
