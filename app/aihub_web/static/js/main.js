// aihub: wires the page together. State lives in one object; each render function redraws one region.

import { ApiError, api, streamChat } from "./api.js";
import { configureConversation, ReplyView, replyFor, userMessage } from "./conversation.js";
import { confirmDialog, openRowMenu, openSettings } from "./dialogs.js";
import { bars, h, icon } from "./dom.js";
import { openChoiceMenu, openEffortMenu } from "./effort.js";
import {
  SHAPES,
  TIER_LABELS,
  barsFor,
  coerceEffort,
  daysLeft,
  levelLabel,
  routeFor,
  shapeNote,
  shortName,
} from "./format.js";
import { openModelPicker } from "./picker.js";
import { renderSidebar } from "./sidebar.js";
import { toast } from "./toast.js";

const $ = (id) => document.getElementById(id);
const els = {
  app: $("app"),
  main: $("main"),
  scrim: $("scrim"),
  sidebar: $("sidebar"),
  nav: $("chat-nav"),
  search: $("search"),
  retention: $("retention"),
  title: $("title"),
  sub: $("title-sub"),
  scroller: $("scroller"),
  route: $("route"),
  routeTitle: $("route-title"),
  routeText: $("route-text"),
  routeRetry: $("route-retry"),
  messages: $("messages"),
  jump: $("jump"),
  form: $("composer"),
  input: $("composer-input"),
  send: $("send"),
  label: $("composer-label"),
  shapeChip: $("shape-chip"),
  shapeName: $("shape-name"),
  modelChip: $("model-chip"),
  modelTier: $("model-tier"),
  modelName: $("model-name"),
  effortChip: $("effort-chip"),
  effortBars: $("effort-bars"),
  effortName: $("effort-name"),
  navOpen: $("nav-open"),
  status: $("status"),
};

const state = {
  models: [],
  byId: new Map(),
  defaultModel: null,
  providers: {},
  chats: [],
  results: null,
  query: "",
  settings: { instructions: "" },
  retentionDays: 30,
  chat: null, // the open chat, or null for a new one
  messages: [],
  draft: { model: null, effort: null, shape: "square" }, // what the next message will use
  drafts: new Map(), // unsent text per chat
  run: null, // the reply in progress
  stick: true, // follow the bottom while a reply streams
  seq: 0,
};

const wide = matchMedia("(min-width: 64rem)");
const coarse = matchMedia("(pointer: coarse)");
const store = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      if (value == null) localStorage.removeItem(key);
      else localStorage.setItem(key, value);
    } catch {
      // Private mode or blocked storage: the setting just won't persist.
    }
  },
};

const currentModel = () => state.byId.get(state.draft.model);
const isImage = (model) => model?.output === "image";
const chatById = (id) => state.chats.find((chat) => chat.id === id);

function announce(text) {
  els.status.textContent = "";
  setTimeout(() => (els.status.textContent = text), 50);
}

function go(hash) {
  if (location.hash === hash || (hash === "#/" && !location.hash)) route();
  else location.hash = hash;
}

// ---- navigation drawer

function setNav(open) {
  const drawer = !wide.matches;
  const losingFocus = !open && els.sidebar.contains(document.activeElement);
  els.app.dataset.nav = open ? "open" : "closed";
  els.navOpen.setAttribute("aria-expanded", String(open));
  els.main.inert = open && drawer;
  els.scrim.hidden = !(open && drawer);
  if (!drawer) store.set("aihub.nav", open ? "open" : "closed");
  if (open && drawer) $("new-chat").focus();
  if (losingFocus) els.navOpen.focus(); // the sidebar is hidden now, and focus must not fall to the page
}

const closeDrawer = () => !wide.matches && els.app.dataset.nav === "open" && setNav(false);

// ---- rendering

function renderSidebarNow() {
  renderSidebar(els.nav, {
    chats: state.results ?? state.chats,
    current: state.chat?.id,
    query: state.query,
  });
}

function renderTitle() {
  const chat = state.chat;
  els.title.textContent = chat ? chat.title : "New chat";
  document.title = chat ? `${chat.title} - aihub` : "aihub";
  const left = chat ? daysLeft(chat.updated_at, state.retentionDays) : null;
  els.sub.hidden = left === null || left > 7;
  if (!els.sub.hidden)
    els.sub.textContent = left === 0 ? "Deletes today" : `Deletes in ${left} day${left === 1 ? "" : "s"}`;
}

function renderRoute() {
  const { title, text } = routeFor(currentModel());
  els.routeTitle.textContent = title;
  els.routeText.textContent = text;
}

function renderChips() {
  const model = currentModel();
  els.modelChip.disabled = !state.models.length;
  els.modelName.textContent = model ? shortName(model.name) : "No model";
  els.modelTier.hidden = !model;
  if (model) {
    els.modelTier.textContent = TIER_LABELS[model.tier];
    els.modelTier.dataset.tier = model.tier;
    els.modelChip.setAttribute("aria-label", `${TIER_LABELS[model.tier]} ${shortName(model.name)}. Change model`);
  }
  const image = isImage(model);
  els.shapeChip.hidden = !image;
  if (image) {
    const { shape } = state.draft;
    els.shapeName.textContent = SHAPES[shape];
    els.shapeChip.setAttribute("aria-label", `Shape: ${shapeNote(model, shape)}. Change shape`);
  }
  els.label.textContent = image ? "Image prompt" : "Message";
  els.input.placeholder = image ? "Describe an image…" : "Message…";
  const efforts = model?.efforts;
  els.effortChip.hidden = !efforts;
  if (efforts) {
    const level = state.draft.effort;
    els.effortBars.replaceChildren(bars(barsFor(level, efforts.levels)));
    els.effortName.textContent = levelLabel(level);
    els.effortChip.setAttribute("aria-label", `Intensity: ${levelLabel(level)}. Change intensity`);
  }
  renderSend();
}

function renderSend() {
  const running = Boolean(state.run);
  els.send.replaceChildren(icon(running ? "stop" : "up"));
  els.send.setAttribute("aria-label", running ? "Stop" : isImage(currentModel()) ? "Create image" : "Send");
  els.send.disabled = !running && (!els.input.value.trim() || !currentModel());
}

function resizeInput() {
  if (CSS.supports("field-sizing", "content")) return;
  els.input.style.height = "auto";
  els.input.style.height = `${els.input.scrollHeight}px`;
}

const idle = (fn) => ("requestIdleCallback" in window ? requestIdleCallback(fn, { timeout: 250 }) : setTimeout(fn, 16));

function renderMessages(messages) {
  const token = ++state.seq;
  els.messages.replaceChildren();
  const last = messages.length - 1;
  const promptFor = (i) => (messages[i - 1]?.role === "user" ? messages[i - 1].content : "");
  const build = (m, i) =>
    m.role === "user" ? userMessage(m) : ReplyView.from(m, { last: i === last, prompt: promptFor(i) }).el;
  // The newest messages first, so the chat is usable at once; the rest fill in above while idle.
  let end = Math.max(0, messages.length - 24);
  els.messages.append(...messages.slice(end).map((m, i) => build(m, end + i)));
  scrollToBottom();
  const older = () => {
    if (token !== state.seq || end <= 0) return;
    const start = Math.max(0, end - 12);
    const before = els.scroller.scrollHeight;
    els.messages.prepend(...messages.slice(start, end).map((m, i) => build(m, start + i)));
    els.scroller.scrollTop += els.scroller.scrollHeight - before;
    end = start;
    idle(older);
  };
  idle(older);
}

function renderChat() {
  els.route.hidden = state.messages.length > 0;
  renderTitle();
  renderRoute();
  renderChips();
  renderSidebarNow();
  renderMessages(state.messages);
}

// ---- scrolling

function nearBottom() {
  const s = els.scroller;
  return s.scrollHeight - s.scrollTop - s.clientHeight < 96;
}

function scrollToBottom() {
  els.scroller.scrollTop = els.scroller.scrollHeight;
  state.stick = true;
  els.jump.hidden = true;
}

function keepPinned() {
  if (state.stick) els.scroller.scrollTop = els.scroller.scrollHeight;
}

els.scroller.addEventListener(
  "scroll",
  () => {
    state.stick = nearBottom();
    els.jump.hidden = state.stick || !state.run;
    els.main.toggleAttribute("data-scrolled", els.scroller.scrollTop > 0);
  },
  { passive: true },
);

// ---- chats

function upsertChat(chat) {
  const at = state.chats.findIndex((c) => c.id === chat.id);
  if (at >= 0) state.chats[at] = { ...state.chats[at], ...chat };
  else state.chats.unshift(chat);
  state.chats.sort((a, b) => b.updated_at - a.updated_at);
  renderSidebarNow();
}

function pickDraft(chat) {
  const stored = chat && state.byId.get(chat.model);
  const model = stored?.available ? stored : state.byId.get(state.defaultModel);
  state.draft = {
    model: model?.id ?? null,
    effort: coerceEffort(model, stored === model ? chat.effort : null),
    shape: state.draft.shape,
  };
}

function newChat() {
  leaveRun();
  state.chat = null;
  state.messages = [];
  pickDraft(null);
  swapDraftText("new");
  renderChat();
  if (!coarse.matches) els.input.focus({ preventScroll: true });
}

async function openChat(id) {
  const seq = ++state.seq;
  leaveRun();
  try {
    const data = await api.get(`/api/chats/${id}`);
    if (seq !== state.seq) return;
    state.chat = data.chat;
    state.messages = data.messages;
    pickDraft(data.chat);
    swapDraftText(id);
    renderChat();
    announce(`Opened ${data.chat.title}${isImage(currentModel()) ? ". Image mode." : ""}`);
    if (data.pending_image) watchImage(data.chat, data.pending_image);
    if (!coarse.matches) els.input.focus({ preventScroll: true });
  } catch (err) {
    if (seq !== state.seq) return;
    toast(err.code === "no_chat" ? "That chat was deleted." : err.message);
    state.chats = state.chats.filter((c) => c.id !== id);
    go("#/");
  }
}

let draftKey = "new";
function swapDraftText(key) {
  state.drafts.set(draftKey, els.input.value);
  draftKey = key;
  els.input.value = state.drafts.get(key) ?? "";
  resizeInput();
  renderSend();
}

const CHAT_HASH = /^#\/c\/([0-9a-f]{16})$/;
// Any other fragment (the skip link's, a typed one) must not replace the chat that is open.
const isRoute = (hash) => CHAT_HASH.test(hash) || hash === "" || hash === "#/";

function route() {
  closeDrawer();
  const match = CHAT_HASH.exec(location.hash);
  if (match) openChat(match[1]);
  else newChat();
}

// ---- replying

/** Moving to another chat ends a text reply, whose connection is the work. An image job outlives its connection. */
function leaveRun() {
  const run = state.run;
  if (!run) return;
  if (!run.image) return run.controller.abort();
  run.detached = true;
  clearTimeout(run.timer);
  run.controller?.abort(); // only stops listening
  if (run.watching) {
    run.view.discard();
    state.run = null;
    renderSend();
  }
}

/** The Stop button. An image job runs on without the request that started it, so Stop is a request of its own. */
function stopRun() {
  const run = state.run;
  if (!run) return;
  if (!run.image) run.controller.abort();
  else if (run.chatId) sendImageStop(run);
  else {
    run.stopWanted = true; // the chat's id arrives with the first event
    run.view.stopping();
  }
}

async function sendImageStop(run) {
  run.view.stopping();
  announce("Stopping the image");
  try {
    await api.post(`/api/chats/${run.chatId}/image/stop`);
  } catch (err) {
    if (err.code === "no_job") return; // it ended a moment ago, and its reply is on the way
    run.view.stopping(false);
    toast(err.message);
  }
}

/** What #status says when an image ends. */
function imageOutcome(message) {
  if (message.status === "stopped") return "Image stopped";
  if (message.status === "error") return `Image failed. ${message.error?.message ?? ""}`.trim();
  const [image] = message.images ?? [];
  if (!image) return "Image ready.";
  return `Image ready. ${image.w} by ${image.h}, ${Math.round((message.usage?.total_ms ?? 0) / 1000)} seconds.`;
}

const POLL_MS = 1500;

/** A chat opened while its image is still being made: show where the job is, and poll until it is stored. */
function watchImage(chat, pending) {
  const view = new ReplyView({
    model: pending.model,
    image: { shape: pending.shape, est: state.byId.get(pending.model)?.est_s?.[pending.shape] },
    onFrame: keepPinned,
  });
  view.progress(pending);
  const run = { image: true, watching: true, started: true, chatId: chat.id, view, timer: 0 };
  els.messages.append(view.el);
  state.run = run;
  renderSend();
  scrollToBottom();
  const poll = async () => {
    try {
      const data = await api.get(`/api/chats/${chat.id}`);
      if (state.run !== run) return;
      if (data.pending_image) {
        view.progress(data.pending_image);
        run.timer = setTimeout(poll, POLL_MS);
        return;
      }
      view.discard();
      state.run = null;
      state.chat = data.chat;
      state.messages = data.messages;
      upsertChat(data.chat);
      renderChat();
      const reply = data.messages.at(-1);
      if (reply?.role === "assistant") announce(imageOutcome(reply));
    } catch (err) {
      if (state.run !== run) return;
      if (err.code === "no_chat") {
        view.discard();
        state.run = null;
        toast("That chat was deleted.");
        go("#/");
        return;
      }
      run.timer = setTimeout(poll, POLL_MS * 2); // the server may be restarting: keep asking
    }
  };
  run.timer = setTimeout(poll, POLL_MS);
}

function localMessage(run, status, error = null) {
  const { view } = run;
  return {
    id: "",
    role: "assistant",
    content: view.text,
    thinking: view.thinking || null,
    model: view.model,
    effort: view.effort,
    usage: {},
    status,
    error,
  };
}

/** Retry redoes the newest reply, so every earlier reply loses its Retry buttons. */
function dropRetryButtons() {
  const replies = els.messages.querySelectorAll(".msg.assistant");
  replies.forEach((reply, i) => {
    if (i < replies.length - 1) reply.querySelectorAll("[data-last-only]").forEach((el) => el.remove());
  });
}

/** Sends a message, or with `regenerateFor` (the id of the user message) answers it again. */
async function startRun({ text = "", regenerateFor = null, replaced = null } = {}) {
  const model = currentModel();
  if (state.run || !model?.available) return;
  const image = isImage(model);
  const { shape } = state.draft;
  const effort = image ? null : state.draft.effort;
  const body = { chat_id: state.chat?.id ?? null, model: model.id, effort };
  if (image) body.shape = shape;
  if (regenerateFor) body.regenerate_for = regenerateFor;
  else body.text = text;
  const prompt = regenerateFor ? (state.messages.findLast((m) => m.role === "user")?.content ?? "") : text;

  const controller = new AbortController();
  const view = new ReplyView({
    waiting: model.tier === "local" ? `Starting ${shortName(model.name)}…` : `Waiting for ${shortName(model.name)}…`,
    model: model.id,
    effort,
    image: image ? { shape, est: model.est_s?.[shape] } : null,
    onFrame: keepPinned,
  });
  const userEl = regenerateFor ? null : userMessage({ id: "", content: text });
  if (userEl) els.messages.append(userEl);
  els.messages.append(view.el);
  els.route.hidden = true;
  const run = {
    controller,
    view,
    userEl,
    text,
    prompt,
    image,
    chatId: state.chat?.id ?? null,
    regenerate: Boolean(regenerateFor),
    replaced,
    started: false,
  };
  state.run = run;
  scrollToBottom();
  renderSend();
  if (image) {
    const est = model.est_s?.[shape];
    announce(est ? `Creating image, about ${est} seconds` : "Creating image");
    run.still = setTimeout(() => announce("Still working"), 15000);
  } else announce("Reply started");

  try {
    for await (const event of streamChat(body, controller.signal)) {
      if (!run.started) {
        run.started = true;
        dropRetryButtons(); // accepted: the older reply is no longer the newest
      }
      onEvent(run, event);
    }
  } catch (err) {
    onRunError(run, err);
  } finally {
    clearTimeout(run.still);
    if (state.run === run) state.run = null;
    renderSend();
    dropRetryButtons();
  }
}

function onEvent(run, event) {
  switch (event.t) {
    case "chat":
      if (!state.chat) {
        state.chat = event.chat;
        draftKey = event.chat.id;
        history.replaceState(null, "", `#/c/${event.chat.id}`);
        renderTitle();
      }
      if (event.user) {
        state.messages.push(event.user);
        if (run.userEl) run.userEl.dataset.id = event.user.id;
      }
      upsertChat(event.chat);
      run.chatId = event.chat.id;
      if (run.stopWanted) sendImageStop(run);
      break;
    case "progress":
      run.view.progress(event);
      break;
    case "think":
      run.view.addThinking(event.d);
      break;
    case "text":
      run.view.addText(event.d);
      break;
    case "note":
      els.messages.insertBefore(
        h(
          "p",
          { class: "notice-inline" },
          `Earlier messages were left out to fit ${run.view.model ? currentModelName(run.view.model) : "the model"}'s context.`,
        ),
        run.view.el,
      );
      break;
    case "done":
    case "error":
      run.view.finish(event.message, { last: true, prompt: run.prompt });
      state.messages.push(event.message);
      if (state.chat) upsertChat({ ...state.chat, updated_at: event.message.created_at || Date.now() });
      if (run.image) announce(imageOutcome(event.message));
      else announce(event.t === "done" ? "Reply finished" : `Reply failed. ${event.message.error?.message ?? ""}`);
      break;
  }
}

function onRunError(run, err) {
  if (err.name === "AbortError") {
    if (run.detached) return run.view.discard(); // an image job carries on without this page
    run.view.finish(localMessage(run, "stopped"), { last: true, prompt: run.prompt });
    announce("Reply stopped");
    return;
  }
  if (!(err instanceof ApiError)) {
    console.error(err);
    toast("Something went wrong in the page. See the console.");
    return;
  }
  if (!run.started) {
    // Refused before anything streamed: undo the optimistic bubbles and give the text back.
    run.view.discard();
    run.userEl?.remove();
    if (run.replaced) {
      els.messages.append(run.replaced.el);
      state.messages = run.replaced.messages;
    }
    if (!run.regenerate && !els.input.value) {
      els.input.value = run.text;
      resizeInput();
    }
    els.route.hidden = state.messages.length > 0 || state.chat !== null;
    const behind = err.code === "stale_chat" && state.chat;
    toast(behind ? `${err.message} It has been reloaded.` : err.message);
    if (behind) go(`#/c/${state.chat.id}`);
    return;
  }
  if (run.image && run.chatId && err.code === "interrupted") {
    // The image job runs on without its connection: reloading the chat shows where it is, or what it made.
    run.view.discard();
    toast("The connection dropped. The image keeps going, so the chat is reloading.");
    go(`#/c/${run.chatId}`);
    return;
  }
  run.view.finish(localMessage(run, "error", { code: err.code, message: err.message }), {
    last: true,
    prompt: run.prompt,
  });
  announce(err.message);
}

function currentModelName(id) {
  const model = state.byId.get(id);
  return model ? shortName(model.name) : id;
}

function send() {
  const text = els.input.value.trim();
  if (!text || state.run) return;
  els.input.value = "";
  state.drafts.set(draftKey, "");
  resizeInput();
  startRun({ text });
}

function retryLast() {
  const last = els.messages.querySelector(".msg.assistant:last-child");
  const question = state.messages.findLast((m) => m.role === "user");
  if (!last || !question || state.run || !state.chat || !currentModel()?.available) return;
  const replaced = { el: last, messages: state.messages };
  last.remove();
  state.messages = state.messages.filter((m, i, all) => !(i === all.length - 1 && m.role === "assistant"));
  startRun({ regenerateFor: question.id, replaced });
}

// ---- choosing a model and an intensity

function chooseModel(afterwards) {
  openModelPicker({
    anchor: els.modelChip,
    models: state.models,
    providers: state.providers,
    selectedId: state.draft.model,
    onSelect: (model) => {
      state.draft.model = model.id;
      state.draft.effort = coerceEffort(model, state.draft.effort);
      renderChips();
      renderRoute();
      afterwards?.();
    },
  });
}

function chooseEffort() {
  const model = currentModel();
  if (!model?.efforts) return;
  openEffortMenu({
    anchor: els.effortChip,
    levels: model.efforts.levels,
    selected: state.draft.effort,
    paid: model.tier === "paid",
    onSelect: (level) => {
      state.draft.effort = level;
      renderChips();
    },
  });
}

function chooseShape() {
  const model = currentModel();
  if (!isImage(model)) return;
  const times = Object.keys(SHAPES)
    .filter((shape) => model.est_s?.[shape])
    .map((shape) => shapeNote(model, shape));
  openChoiceMenu({
    anchor: els.shapeChip,
    title: "Shape",
    choices: Object.entries(SHAPES),
    selected: state.draft.shape,
    text: times.length ? `${times.join(". ")}.` : "",
    onSelect: (shape) => {
      state.draft.shape = shape;
      renderChips();
    },
  });
}

/** "New seed" and "Redo with": the same prompt as a new turn, on the model and in the shape named. */
function makeAgain({ prompt, model, shape }) {
  if (state.run) return toast("Wait for the current reply to finish.");
  const next = state.byId.get(model);
  if (!next?.available) return toast(next?.reason ?? "That model isn't available.");
  state.draft = { model: next.id, effort: coerceEffort(next, null), shape };
  renderChips();
  renderRoute();
  startRun({ text: prompt });
}

/** "Reuse prompt": the words go back in the box to edit. */
function reusePrompt(prompt) {
  els.input.value = prompt;
  resizeInput();
  renderSend();
  els.input.focus();
  els.input.setSelectionRange(prompt.length, prompt.length);
}

// ---- settings and chat actions

function applyTheme(value) {
  if (value === "system") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = value;
  store.set("aihub.theme", value === "system" ? null : value);
}

function connectionRows() {
  const available = (provider) => state.models.filter((m) => m.provider === provider && m.available).length;
  const reason = (provider) => state.models.find((m) => m.provider === provider && !m.available)?.reason;
  const cloud = (provider) =>
    available(provider) ? `Key set, ${available(provider)} models available` : (reason(provider) ?? "Not configured");
  const rows = [
    [
      "Ollama",
      state.providers.ollama?.ok
        ? `Running, ${available("ollama")} models`
        : "Not running. Start it with systemctl --user start ollama.",
    ],
    ["OpenRouter", cloud("openrouter")],
    ["Anthropic", cloud("anthropic")],
  ];
  if (state.providers.images) {
    const makers = state.models.filter(isImage);
    rows.push([
      "Images",
      state.providers.images === "ok"
        ? `Ready, ${makers.length} models`
        : (makers.find((m) => !m.available)?.reason ?? "Not available"),
    ]);
  }
  return rows;
}

async function deleteChat(chat) {
  const ok = await confirmDialog({
    title: "Delete this chat?",
    text: `“${chat.title}” and its messages will be removed from this PC. This can't be undone.`,
    confirm: "Delete chat",
  });
  if (!ok) return;
  try {
    await api.del(`/api/chats/${chat.id}`);
  } catch (err) {
    if (err.code !== "no_chat") return toast(err.message);
  }
  state.chats = state.chats.filter((c) => c.id !== chat.id);
  if (state.results) state.results = state.results.filter((c) => c.id !== chat.id);
  if (state.chat?.id === chat.id) go("#/");
  else renderSidebarNow();
  toast("Chat deleted.");
}

function beginRename(row, chat) {
  const link = row.querySelector(".chat-link");
  const input = h("input", { class: "rename", value: chat.title, maxlength: "200", "aria-label": "Chat title" });
  link.replaceWith(input);
  row.querySelector(".menu-btn").hidden = true;
  input.focus();
  input.select();
  let finished = false;
  const finish = async (save, refocus = false) => {
    if (finished) return;
    finished = true;
    const title = input.value.trim();
    if (save && title && title !== chat.title) {
      try {
        const updated = await api.patch(`/api/chats/${chat.id}`, { title });
        upsertChat(updated);
        if (state.chat?.id === chat.id) {
          state.chat = { ...state.chat, title: updated.title };
          renderTitle();
        }
      } catch (err) {
        toast(err.message);
      }
    }
    renderSidebarNow();
    if (refocus) els.nav.querySelector(`.chat-row[data-id="${chat.id}"] .chat-link`)?.focus();
  };
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") finish(true, true);
    else if (event.key === "Escape") {
      event.stopPropagation(); // not also "close the drawer"
      finish(false, true);
    }
  });
  input.addEventListener("blur", () => finish(true));
}

function openSettingsDialog() {
  let deleted = false;
  const dialog = openSettings({
    theme: store.get("aihub.theme") ?? "system",
    instructions: state.settings.instructions,
    retentionDays: state.retentionDays,
    connections: connectionRows(),
    onTheme: applyTheme,
    onSave: async (text) => {
      state.settings = await api.put("/api/settings", { instructions: text });
    },
    onDeleteAll: async () => {
      const ok = await confirmDialog({
        title: "Delete all chats?",
        text: "Every chat and message will be removed from this PC. This can't be undone.",
        confirm: "Delete everything",
      });
      if (!ok) return false;
      await api.del("/api/chats", { confirm: "delete-all" });
      state.chats = [];
      state.results = null;
      go("#/");
      renderSidebarNow();
      deleted = true;
      return true;
    },
  });
  // Said once the dialog is gone: while it is open the rest of the page is inert and nothing outside it is read out.
  dialog.addEventListener("close", () => deleted && toast("All chats deleted."));
}

// ---- wiring

function decorate() {
  $("nav-close").replaceChildren(icon("panel"));
  $("nav-open").replaceChildren(icon("menu"));
  $("new-chat").replaceChildren(icon("plus"), h("span", {}, "New chat"));
  $("new-chat-top").replaceChildren(icon("plus"));
  $("open-settings").replaceChildren(icon("sliders"), h("span", {}, "Settings"));
  els.jump.replaceChildren(icon("down"), h("span", {}, "Jump to latest"));
  els.send.replaceChildren(icon("up"));
}

function wire() {
  els.navOpen.addEventListener("click", () => setNav(true));
  $("nav-close").addEventListener("click", () => setNav(false));
  els.scrim.addEventListener("click", () => setNav(false));
  $("new-chat").addEventListener("click", () => go("#/"));
  $("new-chat-top").addEventListener("click", () => go("#/"));
  $("open-settings").addEventListener("click", openSettingsDialog);
  els.jump.addEventListener("click", scrollToBottom);
  window.addEventListener("hashchange", () => isRoute(location.hash) && route());
  document.querySelector(".skip").addEventListener("click", (event) => {
    event.preventDefault(); // a fragment jump would leave the chat
    closeDrawer(); // the page behind an open drawer is inert and cannot take focus
    els.input.focus();
  });
  wide.addEventListener("change", () => setNav(wide.matches ? store.get("aihub.nav") !== "closed" : false));

  els.modelChip.addEventListener("click", () => chooseModel());
  els.effortChip.addEventListener("click", chooseEffort);
  els.shapeChip.addEventListener("click", chooseShape);

  els.form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (state.run) stopRun();
    else send();
  });
  els.input.addEventListener("input", () => {
    resizeInput();
    renderSend();
  });
  els.input.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    const modified = event.ctrlKey || event.metaKey;
    if (coarse.matches && !modified) return; // on a phone, Enter is a new line
    event.preventDefault();
    if (!state.run) send();
  });

  let searchTimer;
  els.search.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      const query = els.search.value.trim();
      if (!query) {
        state.results = null;
        state.query = "";
        return renderSidebarNow();
      }
      try {
        const { chats } = await api.get(`/api/chats?q=${encodeURIComponent(query)}`);
        if (query !== els.search.value.trim()) return;
        state.results = chats;
        state.query = query;
        renderSidebarNow();
      } catch (err) {
        toast(err.message);
      }
    }, 160);
  });

  els.nav.addEventListener("click", (event) => {
    const menu = event.target.closest("[data-menu]");
    if (menu) {
      const chat = chatById(menu.dataset.menu) ?? state.results?.find((c) => c.id === menu.dataset.menu);
      if (chat) {
        openRowMenu({
          anchor: menu,
          onRename: () => beginRename(menu.closest(".chat-row"), chat),
          onDelete: () => deleteChat(chat),
        });
      }
    } else if (event.target.closest(".chat-link")) {
      closeDrawer();
    }
  });

  els.messages.addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy], [data-copy-reply]");
    if (!copy) return;
    const text = copy.matches("[data-copy]")
      ? copy.closest(".code")?.querySelector("pre")?.textContent
      : replyFor(copy.closest(".msg"))?.copyable;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      const label = copy.querySelector("span");
      if (label) {
        label.textContent = "Copied";
        setTimeout(() => (label.textContent = "Copy"), 1500);
      }
    } catch {
      toast("The browser blocked copying. Select the text and copy it by hand.");
    }
  });

  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
      event.preventDefault();
      if (els.app.dataset.nav === "closed" || !wide.matches) setNav(true);
      els.search.focus();
    } else if (event.key === "Escape" && !wide.matches && els.app.dataset.nav === "open") {
      setNav(false);
      els.navOpen.focus();
    }
  });
}

function applyBootstrap(data) {
  state.models = data.models;
  state.byId = new Map(data.models.map((m) => [m.id, m]));
  state.defaultModel = data.default_model;
  state.providers = data.providers;
  state.chats = data.chats;
  state.settings = data.settings;
  state.retentionDays = data.retention_days;
  els.retention.textContent = `Chats are deleted ${data.retention_days} days after their last message.`;
  configureConversation({
    modelName: currentModelName,
    levelsFor: (id) => state.byId.get(id)?.efforts?.levels ?? [],
    onRetry: retryLast,
    onChoose: () => chooseModel(retryLast),
    imageModels: () => state.models.filter((m) => isImage(m) && m.available),
    onAgain: makeAgain,
    onReuse: reusePrompt,
  });
}

let loading = false;

async function load() {
  if (loading) return;
  loading = true;
  els.routeTitle.textContent = "";
  els.routeText.textContent = "Loading models and chats…";
  try {
    applyBootstrap(await api.get("/api/bootstrap"));
  } catch (err) {
    els.nav.setAttribute("aria-busy", "false");
    els.modelName.textContent = "No models";
    els.routeTitle.textContent = "Couldn't load aihub";
    els.routeText.textContent = err.message;
    els.routeRetry.hidden = false; // stays visible, and focused, while a retry is in flight
    return;
  } finally {
    loading = false;
  }
  els.routeRetry.hidden = true;
  route();
}

function boot() {
  decorate();
  wire();
  els.routeRetry.addEventListener("click", load);
  setNav(wide.matches ? store.get("aihub.nav") !== "closed" : false);
  load();
}

boot();
