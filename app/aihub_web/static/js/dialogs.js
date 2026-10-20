// Modal dialogs (settings, confirmation) and the small menu on a chat row.

import { h, icon, placePopover } from "./dom.js";

let counter = 0;

function modal(title, body, footer) {
  const titleId = `dlg-title-${++counter}`;
  const close = h("button", { class: "icon-btn", type: "button", "aria-label": "Close" }, icon("x"));
  const dialog = h(
    "dialog",
    { "aria-labelledby": titleId },
    h("div", { class: "dlg-head" }, h("h2", { id: titleId }, title), close),
    body,
    footer,
  );
  close.addEventListener("click", () => dialog.close());
  // The dialog fills its own box, so a click that lands on it directly landed on the backdrop.
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close();
  });
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}

/** Resolves true only if the person confirms. Escape, the backdrop and Cancel all mean no. */
export function confirmDialog({ title, text, confirm, kind = "danger" }) {
  return new Promise((resolve) => {
    let answer = false;
    const yes = h("button", { class: "btn", type: "button", "data-kind": kind }, confirm);
    const no = h("button", { class: "btn", type: "button" }, "Cancel");
    const dialog = modal(
      title,
      h("div", { class: "dlg-body" }, h("p", {}, text)),
      h("div", { class: "dlg-foot" }, no, yes),
    );
    no.addEventListener("click", () => dialog.close());
    yes.addEventListener("click", () => {
      answer = true;
      dialog.close();
    });
    dialog.addEventListener("close", () => resolve(answer));
    no.focus();
  });
}

const THEMES = [
  ["system", "System"],
  ["light", "Light"],
  ["dark", "Dark"],
];

export function openSettings({ theme, instructions, retentionDays, connections, onTheme, onSave, onDeleteAll }) {
  const area = h("textarea", {
    id: "instructions",
    maxlength: "4000",
    placeholder: "Answer briefly. Use metric units.",
    spellcheck: "true",
  });
  area.value = instructions;

  const themeField = h(
    "div",
    { class: "field", role: "radiogroup", "aria-labelledby": "theme-label" },
    h("span", { class: "label", id: "theme-label" }, "Theme"),
    h(
      "div",
      { class: "seg" },
      THEMES.map(([value, label]) =>
        h(
          "label",
          {},
          h("input", { type: "radio", name: "theme", value, checked: value === theme }),
          h("span", {}, label),
        ),
      ),
    ),
  );
  themeField.addEventListener("change", (event) => onTheme(event.target.value));

  const save = h("button", { class: "btn", type: "button", "data-kind": "primary" }, "Save instructions");
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      await onSave(area.value);
    } finally {
      save.disabled = false;
    }
  });

  const wipe = h("button", { class: "btn", type: "button", "data-kind": "danger" }, "Delete all chats…");
  wipe.addEventListener("click", async () => {
    if (await onDeleteAll()) dialog.close();
  });

  const dialog = modal(
    "Settings",
    h(
      "div",
      { class: "dlg-body" },
      themeField,
      h(
        "div",
        { class: "field" },
        h("label", { for: "instructions" }, "Standing instructions"),
        area,
        h("span", { class: "hint" }, "Added to the start of every chat. Up to 4,000 characters."),
        h("div", {}, save),
      ),
      h(
        "div",
        { class: "field" },
        h("span", { class: "label" }, "Connections"),
        h(
          "dl",
          { class: "conn" },
          connections.map(([name, state]) => h("div", {}, h("dt", {}, name), h("dd", {}, state))),
        ),
      ),
      h(
        "div",
        { class: "field" },
        h("span", { class: "label" }, "Your chats"),
        h(
          "span",
          { class: "hint" },
          `Stored only on this PC. Each chat is deleted ${retentionDays} days after its last message.`,
        ),
        h("div", {}, wipe),
      ),
    ),
    h("div", { class: "dlg-foot" }),
  );
  return dialog;
}

let menu;
let menuAnchor;

/** The Rename / Delete menu on a chat row. */
export function openRowMenu({ anchor, onRename, onDelete }) {
  if (!menu) {
    menu = h("div", {
      class: "pop pop-menu",
      id: "row-menu",
      popover: "auto",
      role: "menu",
      "aria-label": "Chat options",
    });
    document.body.append(menu);
    menu.addEventListener("toggle", (event) => {
      if (event.newState === "closed") menuAnchor?.setAttribute("aria-expanded", "false");
    });
    menu.addEventListener("keydown", (event) => {
      const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
      if (!step) return;
      const items = [...menu.querySelectorAll("[role=menuitem]")];
      items[(items.indexOf(document.activeElement) + step + items.length) % items.length].focus();
      event.preventDefault();
    });
  }
  menuAnchor = anchor;
  const item = (name, label, run) =>
    h(
      "button",
      {
        class: "level",
        type: "button",
        role: "menuitem",
        onClick: () => {
          menu.hidePopover();
          run();
        },
      },
      icon(name),
      h("span", {}, label),
    );
  menu.replaceChildren(item("pencil", "Rename…", onRename), item("trash", "Delete…", onDelete));
  anchor.setAttribute("aria-expanded", "true");
  menu.showPopover();
  placePopover(menu, anchor, { below: true });
  menu.querySelector("[role=menuitem]").focus();
}
