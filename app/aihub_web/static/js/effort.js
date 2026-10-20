// The intensity menu: how hard the model should think. Levels are whatever the chosen model offers.

import { bars, h, icon, placePopover } from "./dom.js";
import { barsFor, levelLabel } from "./format.js";

let pop;
let group;
let note;
let current; // { anchor, levels, selected, onSelect }
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
    const level = event.target.closest("[data-level]")?.dataset.level;
    if (!level) return;
    pop.hidePopover();
    current.onSelect(level);
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

export function openEffortMenu(options) {
  if (!pop) build();
  if (closedAt && performance.now() - closedAt < 250) return;
  current = options;
  const { levels, selected } = options;
  group.replaceChildren(
    ...levels.map((level) =>
      h(
        "button",
        {
          class: "level",
          type: "button",
          role: "radio",
          "data-level": level,
          "aria-checked": String(level === selected),
          tabindex: level === selected ? "0" : "-1",
        },
        bars(barsFor(level, levels)),
        h("span", {}, levelLabel(level)),
        level === selected ? icon("check") : null,
      ),
    ),
  );
  note.textContent = options.paid
    ? "Higher intensity thinks longer and costs more."
    : "Higher intensity thinks longer before it answers.";
  options.anchor.setAttribute("aria-expanded", "true");
  pop.showPopover();
  placePopover(pop, options.anchor);
  group.querySelector('[aria-checked="true"]')?.focus();
}
