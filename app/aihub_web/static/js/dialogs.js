// Modal dialogs (settings, confirmation) and the small menu on a chat row.

import { h, icon, placePopover } from "./dom.js";
import { estimateTokens, levelLabel, shortName } from "./format.js";
import { DAY_NAMES, REPEATS, needsDay, needsTime, toFields, toSchedule } from "./schedule.js";

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

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

/** The dialog for a project's name and instructions. `onSave({ name, instructions })` may throw; its message is shown. */
export function openProjectDialog({ project = null, onSave }) {
  const name = h("input", {
    id: "project-name",
    type: "text",
    maxlength: "80",
    autocomplete: "off",
    spellcheck: "false",
    "aria-describedby": "project-status",
  });
  const instructions = h("textarea", {
    id: "project-instructions",
    maxlength: "4000",
    placeholder: "Answer in French. Keep it short.",
    spellcheck: "true",
    "aria-describedby": "project-hint",
  });
  name.value = project?.name ?? "";
  instructions.value = project?.instructions ?? "";
  const hint = h("span", { class: "hint", id: "project-hint" });
  const status = h("p", { class: "action-status", role: "status", id: "project-status" });
  const sayCount = () => {
    const text = instructions.value.trim();
    hint.textContent =
      "Added to every chat in this project, after your standing instructions. Up to 4,000 characters." +
      (text ? ` About ${plural(estimateTokens(text), "token", "tokens")} of the model's memory.` : "");
  };
  sayCount();
  instructions.addEventListener("input", sayCount);

  const form = h(
    "form",
    { class: "dlg-body", id: "project-form", novalidate: "" },
    h("div", { class: "field" }, h("label", { for: "project-name" }, "Name"), name),
    h("div", { class: "field" }, h("label", { for: "project-instructions" }, "Instructions"), instructions, hint),
    status,
  );
  const cancel = h("button", { class: "btn", type: "button" }, "Cancel");
  // Not disabled while it saves: a focused button that turns disabled drops the keyboard out of the dialog.
  const save = h("button", { class: "btn", type: "submit", form: "project-form", "data-kind": "primary" }, "Save");
  const dialog = modal(project ? "Edit project" : "New project", form, h("div", { class: "dlg-foot" }, cancel, save));
  cancel.addEventListener("click", () => dialog.close());
  let saving = false;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving) return;
    const cleaned = name.value.split(/\s+/).filter(Boolean).join(" ");
    status.removeAttribute("data-error");
    if (!cleaned) {
      status.textContent = "Give the project a name.";
      status.setAttribute("data-error", "");
      name.focus();
      return;
    }
    saving = true;
    status.textContent = "";
    try {
      await onSave({ name: cleaned, instructions: instructions.value.trim() });
      dialog.close();
    } catch (err) {
      status.textContent = err.message;
      status.setAttribute("data-error", "");
    } finally {
      saving = false;
    }
  });
  name.focus();
  return dialog;
}

/** Resolves null if the person cancels, else `{ chats: "keep" | "delete" }`. `chatCount` is how many chats it holds. */
export function openDeleteProject({ project, chatCount }) {
  return new Promise((resolve) => {
    let answer = null;
    const also = chatCount ? h("input", { type: "checkbox", id: "also-delete-chats" }) : null;
    const yes = h("button", { class: "btn", type: "button", "data-kind": "danger" }, "Delete project");
    const no = h("button", { class: "btn", type: "button" }, "Cancel");
    const stays = chatCount
      ? `Its ${plural(chatCount, "chat stays", "chats stay")} in your list, without a project.`
      : "It has no chats.";
    const dialog = modal(
      "Delete this project?",
      h(
        "div",
        { class: "dlg-body" },
        h("p", {}, `“${project.name}” will be deleted. ${stays}`),
        also
          ? h(
              "label",
              { class: "check" },
              also,
              h("span", {}, `Also delete its ${plural(chatCount, "chat", "chats")}`),
              h("span", { class: "hint" }, "This can't be undone."),
            )
          : null,
      ),
      h("div", { class: "dlg-foot" }, no, yes),
    );
    no.addEventListener("click", () => dialog.close());
    yes.addEventListener("click", () => {
      answer = { chats: also?.checked ? "delete" : "keep" };
      dialog.close();
    });
    dialog.addEventListener("close", () => resolve(answer));
    no.focus();
  });
}

/** A button with a line beside it that says what happened. The line exists before it speaks, so it is read out. */
function withStatus(button) {
  const status = h("span", { class: "action-status", role: "status" });
  return {
    row: h("div", { class: "action" }, button, status),
    say(text, failed = false) {
      status.textContent = text;
      status.toggleAttribute("data-error", failed);
    },
  };
}

const sameValue = (a, b) =>
  JSON.stringify(a && typeof a === "object" ? Object.entries(a).sort() : a) ===
  JSON.stringify(b && typeof b === "object" ? Object.entries(b).sort() : b);

/**
 * The dialog for a scheduled task. `models` are the models a task may use (text models on this PC) and `projects`
 * the projects. `onSave(body)` may throw; its message is shown. For a task that exists the body holds only what the
 * person changed, and is empty when nothing was.
 */
export function openTaskDialog({ task = null, models, projects, onSave }) {
  const options = models.map((m) => ({
    id: m.id,
    text: shortName(m.name) + (m.available ? "" : " (not available now)"),
    levels: m.efforts?.levels ?? [],
    available: m.available,
  }));
  // A model that was removed since the task was made stays what it is, rather than being swapped for another.
  if (task && !options.some((o) => o.id === task.model)) {
    options.push({
      id: task.model,
      text: `${task.model.replace(/^ollama\//, "")} (not installed)`,
      levels: [],
      available: false,
    });
  }

  const name = h("input", { id: "task-name", type: "text", maxlength: "80", autocomplete: "off", spellcheck: "false" });
  const prompt = h("textarea", {
    id: "task-prompt",
    placeholder: "Summarise what I should do today.",
    spellcheck: "true",
    "aria-describedby": "task-prompt-hint",
  });
  const model = h("select", { id: "task-model", "aria-describedby": "task-model-hint" });
  const effort = h("select", { id: "task-effort" });
  const repeat = h(
    "select",
    { id: "task-repeat", "aria-describedby": "task-repeat-hint" },
    REPEATS.map(([value, words]) => h("option", { value }, words)),
  );
  const time = h("input", { id: "task-time", type: "time" });
  const day = h(
    "select",
    { id: "task-day" },
    DAY_NAMES.map((words, i) => h("option", { value: String(i) }, words)),
  );
  const project = h(
    "select",
    { id: "task-project" },
    h("option", { value: "" }, "No project"),
    projects.map((p) => h("option", { value: p.id }, p.name)),
  );
  const enabled = h("input", { id: "task-enabled", type: "checkbox" });

  const start = toFields(task?.schedule);
  name.value = task?.name ?? "";
  prompt.value = task?.prompt ?? "";
  model.replaceChildren(...options.map((o) => h("option", { value: o.id }, o.text)));
  model.value = task?.model ?? (options.find((o) => o.available) ?? options[0])?.id ?? "";
  repeat.value = start.repeat;
  time.value = start.time;
  day.value = start.day;
  project.value = task?.project_id ?? "";
  enabled.checked = task ? task.enabled : true;

  const effortField = h("div", { class: "field" }, h("label", { for: "task-effort" }, "Intensity"), effort);
  const timeField = h("div", { class: "field" }, h("label", { for: "task-time" }, "Time"), time);
  const dayField = h("div", { class: "field" }, h("label", { for: "task-day" }, "Day"), day);
  const when = h("div", { class: "field-row" }, timeField, dayField);

  const syncEffort = (keep) => {
    const levels = options.find((o) => o.id === model.value)?.levels ?? [];
    effort.replaceChildren(
      h("option", { value: "" }, "Default"),
      ...levels.map((level) => h("option", { value: level }, levelLabel(level))),
    );
    effort.value = levels.includes(keep) ? keep : "";
    effortField.hidden = levels.length === 0;
  };
  const syncSchedule = () => {
    timeField.hidden = !needsTime(repeat.value);
    dayField.hidden = !needsDay(repeat.value);
    when.hidden = timeField.hidden && dayField.hidden;
  };
  syncEffort(task?.effort ?? "");
  syncSchedule();
  model.addEventListener("change", () => syncEffort(effort.value));
  repeat.addEventListener("change", syncSchedule);

  const status = h("p", { class: "action-status", role: "status", id: "task-status" });
  const form = h(
    "form",
    { class: "dlg-body", id: "task-form", novalidate: "" },
    h("div", { class: "field" }, h("label", { for: "task-name" }, "Name"), name),
    h(
      "div",
      { class: "field" },
      h("label", { for: "task-prompt" }, "Prompt"),
      prompt,
      h(
        "span",
        { class: "hint", id: "task-prompt-hint" },
        "Sent as the message. If the task is in a project, the project's instructions are added.",
      ),
    ),
    h(
      "div",
      { class: "field" },
      h("label", { for: "task-model" }, "Model"),
      model,
      h(
        "span",
        { class: "hint", id: "task-model-hint" },
        options.length
          ? "Tasks use models on this PC, so they never cost anything."
          : "No model on this PC was found. Start Ollama and reload the page.",
      ),
    ),
    effortField,
    h(
      "div",
      { class: "field" },
      h("label", { for: "task-repeat" }, "Repeat"),
      repeat,
      h(
        "span",
        { class: "hint", id: "task-repeat-hint" },
        "On this PC's clock. A run missed while aihub was closed happens when it opens, if that is within 12 hours.",
      ),
    ),
    when,
    h("div", { class: "field" }, h("label", { for: "task-project" }, "Project"), project),
    h("label", { class: "check" }, enabled, h("span", {}, "Run on this schedule")),
  );
  const cancel = h("button", { class: "btn", type: "button" }, "Cancel");
  // Not disabled while it saves: a focused button that turns disabled drops the keyboard out of the dialog.
  const save = h("button", { class: "btn", type: "submit", form: "task-form", "data-kind": "primary" }, "Save");
  // In the footer, which stays in view when the form scrolls: a message below the fold is read by a screen reader and missed by everyone else.
  const dialog = modal(task ? "Edit task" : "New task", form, h("div", { class: "dlg-foot" }, status, cancel, save));
  cancel.addEventListener("click", () => dialog.close());
  let saving = false;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (saving) return;
    const cleaned = name.value.split(/\s+/).filter(Boolean).join(" ");
    const text = prompt.value.trim();
    const problem = !cleaned
      ? ["Give the task a name.", name]
      : !text
        ? ["Write the prompt.", prompt]
        : !model.value
          ? ["Choose a model.", model]
          : needsTime(repeat.value) && !time.value
            ? ["Choose a time.", time]
            : null;
    status.removeAttribute("data-error");
    if (problem) {
      status.textContent = problem[0];
      status.setAttribute("data-error", "");
      problem[1].focus();
      return;
    }
    const wanted = {
      name: cleaned,
      prompt: text,
      model: model.value,
      // A model the page does not know shows no levels, and must not wipe the one the task has.
      effort: effortField.hidden ? (task?.effort ?? null) : effort.value || null,
      schedule: toSchedule({ repeat: repeat.value, time: time.value, day: day.value }),
      project_id: project.value || null,
      enabled: enabled.checked,
    };
    const body = task
      ? Object.fromEntries(Object.entries(wanted).filter(([key, value]) => !sameValue(value, task[key])))
      : wanted;
    saving = true;
    status.textContent = "";
    try {
      await onSave(body);
      dialog.close();
    } catch (err) {
      status.textContent = err.message;
      status.setAttribute("data-error", "");
    } finally {
      saving = false;
    }
  });
  name.focus();
  return dialog;
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

  // Not disabled while it works: a focused button that turns disabled drops the keyboard out of the dialog.
  const save = h("button", { class: "btn", type: "button", "data-kind": "primary" }, "Save instructions");
  const saved = withStatus(save);
  let saving = false;
  save.addEventListener("click", async () => {
    if (saving) return;
    saving = true;
    saved.say("");
    try {
      await onSave(area.value);
      saved.say("Saved.");
    } catch (err) {
      saved.say(err.message, true);
    } finally {
      saving = false;
    }
  });
  area.addEventListener("input", () => saved.say(""));

  const wipe = h("button", { class: "btn", type: "button", "data-kind": "danger" }, "Delete all chats…");
  const wiped = withStatus(wipe);
  wipe.addEventListener("click", async () => {
    wiped.say("");
    try {
      if (await onDeleteAll()) dialog.close();
    } catch (err) {
      wiped.say(err.message, true);
    }
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
        saved.row,
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
        wiped.row,
      ),
    ),
    h("div", { class: "dlg-foot" }),
  );
  return dialog;
}

let menu;
let menuAnchor;

/** The small menu on a chat or project row: `items` are { icon, label, run }. */
export function openRowMenu({ anchor, label, items }) {
  if (!menu) {
    menu = h("div", {
      class: "pop pop-menu",
      id: "row-menu",
      popover: "auto",
      role: "menu",
    });
    document.body.append(menu);
    menu.addEventListener("toggle", (event) => {
      if (event.newState === "closed") menuAnchor?.setAttribute("aria-expanded", "false");
    });
    menu.addEventListener("keydown", (event) => {
      const step = { ArrowDown: 1, ArrowUp: -1 }[event.key];
      if (!step) return;
      const entries = [...menu.querySelectorAll("[role=menuitem]")];
      entries[(entries.indexOf(document.activeElement) + step + entries.length) % entries.length].focus();
      event.preventDefault();
    });
  }
  menuAnchor = anchor;
  menu.setAttribute("aria-label", label);
  menu.replaceChildren(
    ...items.map((item) =>
      h(
        "button",
        {
          class: "level",
          type: "button",
          role: "menuitem",
          onClick: () => {
            menu.hidePopover();
            item.run();
          },
        },
        icon(item.icon),
        h("span", {}, item.label),
      ),
    ),
  );
  anchor.setAttribute("aria-expanded", "true");
  menu.showPopover();
  placePopover(menu, anchor, { below: true });
  menu.querySelector("[role=menuitem]").focus();
}
