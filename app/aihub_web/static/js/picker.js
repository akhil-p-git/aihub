// The model picker: a searchable list grouped by where a prompt goes. A popover on wide screens, a sheet on phones.

import { h, icon, placePopover } from "./dom.js";
import { formatBytes, formatContext, formatDay, formatPrice } from "./format.js";

// [key, label, which models]. Image models are local too, but they sit in a group of their own.
const makesImages = (m) => m.output === "image";
const GROUPS = [
  ["free", "Free, in the cloud", (m) => m.tier === "free"],
  ["local", "On this PC", (m) => m.tier === "local" && !makesImages(m)],
  ["images", "Makes images", (m) => m.tier === "local" && makesImages(m)],
  ["paid", "Paid, billed to your credits", (m) => m.tier === "paid"],
];

let pop;
let search;
let list;
let current; // { anchor, models, providers, selectedId, onSelect }
let closedAt = 0;

function subtitle(model) {
  if (!model.available) return [model.reason];
  if (makesImages(model)) return [model.note, "Free, on this PC"].filter(Boolean);
  const parts = [];
  if (model.size_bytes) parts.push(formatBytes(model.size_bytes));
  if (model.ctx) parts.push(`${formatContext(model.ctx)} context`);
  if (model.vision) parts.push("Reads images");
  if (model.efforts) parts.push("Adjustable reasoning");
  if (model.expires) parts.push(`Free until ${formatDay(model.expires)}`);
  if (model.price) parts.push(formatPrice(model.price));
  return parts;
}

function option(model, index) {
  const selected = model.id === current.selectedId;
  const row = h(
    "div",
    {
      class: "opt",
      role: "option",
      id: `model-opt-${index}`,
      "data-model": model.id,
      "aria-selected": String(selected),
      "aria-disabled": String(!model.available),
    },
    h("span", { class: "opt-name" }, model.name),
    selected ? h("span", { class: "opt-check" }, icon("check")) : null,
    h("span", { class: "opt-sub" }, ...subtitle(model).map((text) => h("span", {}, text))),
  );
  return row;
}

function missingNotes(shown) {
  const { providers = {} } = current;
  const notes = [];
  if (!shown.has("local") && providers.ollama?.ok === false) {
    notes.push("Ollama isn't running. Start it with systemctl --user start ollama.");
  }
  if (!shown.has("free") && providers.openrouter?.ok === false) {
    notes.push("Couldn't load OpenRouter's model list. Check the connection.");
  }
  return notes.map((text) => h("p", { class: "pop-empty" }, text));
}

function renderList() {
  const words = search.value.toLowerCase().split(/\s+/).filter(Boolean);
  const matches = (m) => {
    const haystack =
      `${m.name} ${m.id} ${m.tier} ${m.provider} ${m.vision ? "reads images vision" : ""} ${makesImages(m) ? "makes images image" : ""}`.toLowerCase();
    return words.every((word) => haystack.includes(word));
  };
  list.replaceChildren();
  const shown = new Set();
  let index = 0;
  for (const [key, label, belongs] of GROUPS) {
    const rows = current.models.filter((m) => belongs(m) && matches(m));
    if (!rows.length) continue;
    shown.add(key);
    const labelId = `model-group-${key}`;
    list.append(
      h(
        "div",
        { role: "group", "aria-labelledby": labelId },
        h("div", { class: "pop-group", id: labelId }, label),
        rows.map((m) => option(m, index++)),
      ),
    );
  }
  if (!words.length) list.append(...missingNotes(shown));
  if (!shown.size && words.length) {
    list.append(h("p", { class: "pop-empty" }, `No model matches “${search.value.trim()}”.`));
  }
  setActive(list.querySelector('[aria-selected="true"]:not([aria-disabled="true"])') ?? selectable()[0]);
}

const selectable = () => [...list.querySelectorAll('.opt:not([aria-disabled="true"])')];

function setActive(opt) {
  list.querySelector("[data-active]")?.removeAttribute("data-active");
  if (!opt) {
    search.removeAttribute("aria-activedescendant");
    return;
  }
  opt.setAttribute("data-active", "");
  search.setAttribute("aria-activedescendant", opt.id);
  opt.scrollIntoView({ block: "nearest" });
}

function move(step) {
  const options = selectable();
  if (!options.length) return;
  const at = options.indexOf(list.querySelector("[data-active]"));
  setActive(options[(at + step + options.length) % options.length]);
}

function choose(opt) {
  if (!opt || opt.getAttribute("aria-disabled") === "true") return;
  const model = current.models.find((m) => m.id === opt.dataset.model);
  pop.hidePopover();
  if (model) current.onSelect(model);
}

function build() {
  search = h("input", {
    type: "search",
    placeholder: "Search models…",
    autocomplete: "off",
    spellcheck: "false",
    role: "combobox",
    "aria-label": "Search models",
    "aria-autocomplete": "list",
    "aria-controls": "model-list",
    "aria-expanded": "true",
  });
  list = h("div", { class: "pop-list", id: "model-list", role: "listbox", "aria-label": "Models" });
  const close = h("button", { class: "icon-btn", type: "button", "aria-label": "Close" }, icon("x"));
  close.addEventListener("click", () => pop.hidePopover());
  pop = h(
    "div",
    { class: "pop", id: "model-pop", popover: "auto", role: "dialog", "aria-label": "Choose a model" },
    h("div", { class: "pop-head" }, h("p", { class: "pop-title", "aria-hidden": "true" }, "Choose a model"), close),
    h("div", { class: "pop-search" }, search),
    list,
  );
  document.body.append(pop);

  search.addEventListener("input", renderList);
  search.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") move(1);
    else if (event.key === "ArrowUp") move(-1);
    else if (event.key === "Enter") choose(list.querySelector("[data-active]"));
    else return;
    event.preventDefault();
  });
  list.addEventListener("click", (event) => choose(event.target.closest(".opt")));
  list.addEventListener("pointermove", (event) => {
    const opt = event.target.closest(".opt");
    if (opt && opt.getAttribute("aria-disabled") !== "true" && !opt.hasAttribute("data-active")) setActive(opt);
  });
  pop.addEventListener("toggle", (event) => {
    if (event.newState !== "closed") return;
    closedAt = performance.now();
    current?.anchor.setAttribute("aria-expanded", "false");
  });
}

export function openModelPicker(options) {
  if (!pop) build();
  // A click on the anchor first light-dismisses the open popover; don't reopen it on the same click.
  if (closedAt && performance.now() - closedAt < 250) return;
  current = options;
  search.value = "";
  renderList();
  options.anchor.setAttribute("aria-expanded", "true");
  pop.showPopover();
  placePopover(pop, options.anchor);
  list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "center" });
  if (!matchMedia("(pointer: coarse)").matches) search.focus();
}
