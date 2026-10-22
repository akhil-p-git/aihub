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
  imageModels: () => [],
  onAgain() {}, // make the prompt again as a new turn: ({ prompt, model, shape })
  onReuse() {}, // put the prompt back in the box
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
  engine_unreachable: "Can't reach the image engine",
  engine_failed_to_start: "The image engine didn't start",
  cuda_unavailable: "The GPU can't make images right now",
  vram_busy: "Not enough free GPU memory",
  vram_busy_unknown: "Not enough free GPU memory",
  engine_crashed: "The image engine crashed",
  engine_failed: "The image engine failed",
  ollama_busy: "A local model is still using the GPU",
  gpu_busy: "The GPU is busy",
  bad_output: "The engine returned something that isn't an image",
};

/** Retry and "Choose another model" redo the newest reply, so only the newest reply offers them (data-last-only). */
function errorNotice(message, isLast) {
  const { code, message: text } = message.error ?? {};
  return h(
    "div",
    { class: "notice", role: "alert" },
    h("strong", {}, icon("alert"), ERROR_TITLES[code] ?? "The reply failed"),
    h("span", {}, text ?? "The reply did not finish."),
    isLast
      ? h(
          "div",
          { class: "row", "data-last-only": true },
          h("button", { class: "btn", type: "button", onClick: () => hooks.onRetry() }, icon("retry"), "Retry"),
          h("button", { class: "btn", type: "button", onClick: () => hooks.onChoose() }, "Choose another model"),
        )
      : null,
  );
}

const slug = (text) =>
  text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .slice(0, 40) || "image";

// A prompt that asks for lettering. Image models spell badly, and the models' notes say which ones.
const ASKS_FOR_TEXT = /["“”]|\b(text|words?|letters?|sign|label|logo|poster|title|caption|writ(?:e|ing|ten)|says?)\b/i;

/** The footer and actions of a finished image: what made it, and what to do with it next. */
function imageMeta(message, prompt, figure) {
  const usage = message.usage ?? {};
  const image = message.images[0];
  const url = `/api/chats/${message.chat_id}/images/${image.id}`;
  const item = (text, title) => h("span", { title }, text);
  const parts = [item(hooks.modelName(message.model))];
  if (usage.total_ms) parts.push(item(`${Math.round(usage.total_ms / 1000)} s`, "Total time"));
  parts.push(item(`${image.steps} steps`), item(`seed ${image.seed}`, "Same seed and prompt give the same image"));
  parts.push(item(`${image.w} by ${image.h}`));
  if (usage.load_ms > 2000)
    parts.push(item(`loaded in ${Math.round(usage.load_ms / 1000)} s`, "Time to load the model"));
  parts.push(item("Free, on this PC"));
  const shape = usage.shape ?? "square";
  const other = hooks.imageModels().find((m) => m.id !== message.model);
  const full = h(
    "button",
    { class: "btn", type: "button", "aria-pressed": "false", "data-full-size": true },
    h("span", {}, "Full size"),
  );
  full.addEventListener("click", () => {
    const on = !figure.hasAttribute("data-full");
    figure.toggleAttribute("data-full", on);
    full.setAttribute("aria-pressed", String(on));
  });
  const button = (label, onClick) => h("button", { class: "btn", type: "button", onClick }, h("span", {}, label));
  const actions = h(
    "span",
    { class: "actions img-actions" },
    h(
      "a",
      {
        class: "btn",
        href: url,
        download: `${slug(prompt)}-${image.seed}-${shape}.webp`,
        "aria-label": `Download image, ${image.w} by ${image.h}`,
      },
      icon("down"),
      h("span", {}, "Download"),
    ),
    button("New seed", () => hooks.onAgain({ prompt, model: message.model, shape })),
    button("Reuse prompt", () => hooks.onReuse(prompt)),
    other ? button(`Redo with ${other.name}`, () => hooks.onAgain({ prompt, model: other.id, shape })) : null,
    full,
  );
  return h("div", { class: "meta" }, parts, actions);
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
          message.content
            ? h(
                "button",
                { class: "btn", type: "button", "data-copy-reply": true, "aria-label": "Copy reply" },
                icon("copy"),
                h("span", {}, "Copy"),
              )
            : null,
          isLast
            ? h(
                "button",
                {
                  class: "btn",
                  type: "button",
                  "data-last-only": true,
                  "aria-label": "Retry reply",
                  onClick: () => hooks.onRetry(),
                },
                icon("retry"),
                h("span", {}, "Retry"),
              )
            : null,
        );
  return h("div", { class: "meta" }, parts, actions);
}

/** Gives keyboard focus to how a reply ended (its image, or its notice), for someone who was on its progress bar. */
export function focusReply(el) {
  const target = el.querySelector(".img-out, .notice") ?? el;
  target.tabIndex = -1;
  target.focus({ preventScroll: true });
}

const registry = new WeakMap();

/** The ReplyView that built a reply element, so the Copy button can reach its text. */
export const replyFor = (el) => registry.get(el);

export class ReplyView {
  /**
   * `waiting` is the line shown until the first token; `onFrame` runs after each batch of updates.
   * `image` ({ shape, est }) makes this the reply to an image prompt: a slate in that shape, with `est` seconds
   * as the model's estimate, stands in until the image arrives.
   */
  constructor({
    waiting = "Waiting for the model…",
    model = null,
    effort = null,
    image = null,
    onFrame = () => {},
  } = {}) {
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
    if (image) this.#openSlate(image);
  }

  /**
   * Where an image has got to: `{ phase, step, steps }`, as the server reports it. A chat that is polled hears the
   * same answer again and again, so only a change counts as progress for the "taking longer than usual" warning.
   */
  progress({ phase, step, steps }) {
    if (!this.slate) return;
    const moved = phase !== this.phase || (step ?? null) !== this.step;
    this.phase = phase;
    this.step = step ?? null;
    this.steps = steps ?? this.steps;
    if (moved) this.lastProgress = performance.now();
    this.#drawSlate();
  }

  /** Says a stop request is on its way (`false` takes it back), until the stream ends with the stopped reply. */
  stopping(on = true) {
    this.stoppingNow = on;
    if (this.slate) this.#drawSlate();
  }

  /** The slate: a box in the image's shape with a signal bar for every second step, so a long job shows life. */
  #openSlate({ shape, est }) {
    this.wait.remove();
    this.est = est ?? null;
    this.slateLabel = h("span", { class: "img-label", "aria-hidden": "true" }, "Preparing…");
    this.signal = h("span", { class: "img-signal", "aria-hidden": "true" });
    this.slate = h(
      "div",
      {
        class: "img-slate",
        "data-shape": shape,
        // Focusable, so a keyboard can scroll to it and a screen reader can ask how far it has got: it is not live.
        tabindex: "0",
        role: "progressbar",
        "aria-live": "off",
        "aria-label": "Making the image",
        "aria-valuemin": "0",
        "aria-valuemax": "100",
        "aria-valuetext": "Preparing…",
      },
      this.signal,
      this.slateLabel,
    );
    this.el.insertBefore(this.slate, this.body);
    this.slateStart = this.lastProgress = performance.now();
    this.timer = setInterval(() => this.#drawSlate(), 1000);
    this.#drawSlate();
  }

  /** The text that fits the phase. Nothing here is announced: the page does that, once, through #status. */
  #slateText() {
    const now = performance.now();
    if (this.stoppingNow) return "Stopping…";
    if (now - this.lastProgress > 30000) {
      return "Taking longer than usual. The GPU may be busy or stuck. Stop to give up.";
    }
    switch (this.phase) {
      case "waiting":
        return "Waiting for the GPU…";
      case "starting":
        return `Starting the image engine… ${Math.round((now - this.lastProgress) / 1000)} s`;
      case "queued":
        return "Queued…";
      case "sampling":
        if (this.est && this.est < 10) return `About ${this.est} s`;
        // Before the first step the engine is loading the model and reading the prompt, which takes seconds.
        return this.step
          ? `Step ${this.step} of ${this.steps}`
          : `Working… ${Math.round((now - this.lastProgress) / 1000)} s`;
      case "finishing":
        return "Finishing…";
      default:
        return "Preparing…";
    }
  }

  #drawSlate() {
    const text = this.#slateText();
    this.slateLabel.textContent = text;
    this.slate.setAttribute("aria-valuetext", text);
    const total = this.steps ?? 8;
    // A job estimated under ten seconds has too few steps to read as progress: its bars loop instead.
    const loop = Boolean(this.est && this.est < 10);
    this.slate.toggleAttribute("data-loop", loop);
    if (this.steps && this.step != null) {
      this.slate.setAttribute("aria-valuenow", String(Math.round((this.step / this.steps) * 100)));
    } else this.slate.removeAttribute("aria-valuenow");
    const count = total > 4 ? Math.ceil(total / 2) : total;
    if (this.signal.childElementCount !== count) {
      // The page's CSP forbids style attributes, so the bars get their place through the CSSOM.
      this.signal.replaceChildren(
        ...Array.from({ length: count }, (_, i) => {
          const bar = h("i");
          bar.style.setProperty("--i", String(i));
          return bar;
        }),
      );
      this.signal.style.setProperty("--n", String(count));
    }
    const lit = this.phase === "finishing" ? count : Math.floor(((this.step ?? 0) / total) * count);
    [...this.signal.children].forEach((bar, i) => bar.toggleAttribute("data-on", i < lit));
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

  /** Drops the reply without ending it: a request that was refused, or a page that moved on. */
  discard() {
    cancelAnimationFrame(this.raf);
    this.#stopTimer();
    this.el.remove();
  }

  /** Ends the reply with the stored message (or a local stand-in when the stream was cut). */
  finish(message, { last = true, prompt = "" } = {}) {
    cancelAnimationFrame(this.raf);
    this.raf = 0;
    this.#stopTimer();
    this.wait.remove();
    const onSlate = Boolean(this.slate?.contains(document.activeElement));
    this.slate?.remove();
    this.slate = null;
    const made = message.status === "done" && message.images?.length ? message.images[0] : null;
    const text = message.content || this.text;
    if (message.thinking || this.thinking)
      this.#settleThinking(message.thinking || this.thinking, message.usage?.think_ms);
    this.render.update(text, true);
    this.drawn = this.text = text;
    this.copyText = text;
    this.el.dataset.id = message.id;
    this.el.dataset.status = message.status;
    if (message.status === "error") this.el.append(errorNotice(message, last));
    else if (message.status === "stopped") {
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
    if (made) {
      const img = h("img", {
        class: "img-out",
        src: `/api/chats/${message.chat_id}/images/${made.id}`,
        width: String(made.w),
        height: String(made.h),
        alt: `Generated image: ${prompt.slice(0, 120)}`,
        decoding: "async",
      });
      img.style.setProperty("--ar", (made.w / made.h).toFixed(4));
      img.addEventListener("load", () => this.onFrame());
      const figure = h("figure", { class: "img-fig" }, img);
      this.el.append(figure);
      const models = hooks.imageModels();
      const better = models.find((m) => m.id !== message.model);
      if (ASKS_FOR_TEXT.test(prompt) && models.find((m) => m.id === message.model)?.note?.includes("misspelled")) {
        this.el.append(
          h(
            "p",
            { class: "notice-inline" },
            "Text in images is often misspelled on this model. ",
            better
              ? h(
                  "button",
                  {
                    class: "btn",
                    type: "button",
                    onClick: () => hooks.onAgain({ prompt, model: better.id, shape: message.usage?.shape ?? "square" }),
                  },
                  `Redo with ${better.name}`,
                )
              : null,
          ),
        );
      }
      this.el.append(imageMeta(message, prompt, figure));
    } else this.el.append(meta(message, last));
    if (onSlate) focusReply(this.el);
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
  static from(message, { last = false, prompt = "" } = {}) {
    const view = new ReplyView({ model: message.model, effort: message.effort });
    view.finish(message, { last, prompt });
    return view;
  }
}
