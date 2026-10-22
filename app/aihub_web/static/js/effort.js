// The small choice menu under a chip: how hard the model should think (levels are whatever the chosen model offers),
// or what shape an image takes. One popover serves both.

import { bars, h, icon, placePopover } from "./dom.js";
import { barsFor, levelLabel } from "./format.js";

let pop;
let group;
let note;
let current; // { anchor, onSelect }
let closedAt = 0;

function build() {
  group = h("div", { role: "radiogroup", "aria-label": "Intensity" });
  note = h("p", { class: "pop-note" });
  pop = h(
    "div",
    { class: "pop pop-effort", id: "effort-pop", popover: "auto", role: "dialog", "aria-label": "Intensity" },
    group,
    note,
  );
  document.body.append(pop);

  group.addEventListener("click", (event) => {
    const value = event.target.closest("[data-level]")?.dataset.level;
    if (!value) return;
    pop.hidePopover();
    current.onSelect(value);
  });
  group.addEventListener("keydown", (event) => {
    const step = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[event.key];
    if (!step) return;
    const radios = [...group.querySelectorAll("[role=radio]")];
    const at = radios.indexOf(document.activeElement);
    radios[(at + step + radios.length) % radios.length].focus();
    event.preventDefault();
  });
  pop.addEventListener("toggle", (event) => {
    if (event.newState !== "closed") return;
    closedAt = performance.now();
    current?.anchor.setAttribute("aria-expanded", "false");
  });
}

/** `rows` are { value, content[] }; `selected` is the value that is checked. */
function show({ anchor, onSelect, title, rows, selected, text, below = false }) {
  if (!pop) build();
  if (closedAt && performance.now() - closedAt < 250) return;
  current = { anchor, onSelect };
  pop.setAttribute("aria-label", title);
  group.setAttribute("aria-label", title);
  group.replaceChildren(
    ...rows.map(({ value, content }) =>
      h(
        "button",
        {
          class: "level",
          type: "button",
          role: "radio",
          "data-level": value,
          "aria-checked": String(value === selected),
          tabindex: value === selected ? "0" : "-1",
        },
        ...content,
        value === selected ? icon("check") : null,
      ),
    ),
  );
  note.textContent = text;
  anchor.setAttribute("aria-expanded", "true");
  pop.showPopover();
  placePopover(pop, anchor, { below });
  group.querySelector('[aria-checked="true"]')?.focus();
}

export function openEffortMenu({ anchor, levels, selected, paid, onSelect }) {
  show({
    anchor,
    onSelect,
    title: "Intensity",
    selected,
    rows: levels.map((level) => ({
      value: level,
      content: [bars(barsFor(level, levels)), h("span", {}, levelLabel(level))],
    })),
    text: paid ? "Higher intensity thinks longer and costs more." : "Higher intensity thinks longer before it answers.",
  });
}

/** `choices` are [value, label] pairs; `text` is the note under them. */
export function openChoiceMenu({ anchor, title, choices, selected, text, onSelect, below }) {
  show({
    anchor,
    onSelect,
    title,
    below,
    selected,
    rows: choices.map(([value, label]) => ({ value, content: [h("span", {}, label)] })),
    text,
  });
}
