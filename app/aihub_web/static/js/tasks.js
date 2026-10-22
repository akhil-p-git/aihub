// The scheduled tasks page: one card for each task.

import { h, working } from "./dom.js";
import { shortName, taskNext, taskStatus } from "./format.js";

const firstLine = (text) =>
  text
    .split("\n")
    .find((line) => line.trim())
    ?.trim() ?? "";

function button(task, action, label) {
  return h(
    "button",
    {
      class: "btn",
      type: "button",
      "data-action": action,
      "data-id": task.id,
      "data-key": `${task.id}:${action}`,
      "aria-label": `${label}: ${task.name}`,
    },
    label,
  );
}

function onOffSwitch(task) {
  return h(
    "label",
    { class: "switch" },
    h("input", {
      type: "checkbox",
      role: "switch",
      checked: task.enabled,
      "data-action": "toggle",
      "data-id": task.id,
      "data-key": `${task.id}:toggle`,
      "aria-labelledby": `task-name-${task.id}`,
    }),
    h("span", { "aria-hidden": "true" }, task.enabled ? "On" : "Off"),
  );
}

function card(task, { byId, projectById, now }) {
  const status = taskStatus(task, now);
  const next = taskNext(task, now);
  const model = byId.get(task.model);
  const project = task.project_id ? projectById(task.project_id) : null;
  return h(
    "li",
    { class: "task", "data-id": task.id },
    h(
      "div",
      { class: "task-top" },
      h("h2", { class: "task-name", id: `task-name-${task.id}` }, task.name),
      onOffSwitch(task),
    ),
    h(
      "p",
      { class: "task-meta" },
      h("span", {}, task.schedule_text),
      h("span", {}, model ? shortName(model.name) : task.model.replace(/^ollama\//, "")),
      project ? h("span", {}, project.name) : null,
    ),
    h("p", { class: "task-prompt" }, firstLine(task.prompt)),
    h(
      "div",
      { class: "task-status", "data-kind": status.kind },
      status.kind === "running"
        ? working(status.label)
        : status.kind === "off"
          ? null // its reason is the whole explanation, and begins with the words
          : h("span", {}, status.label),
      status.reason ? h("span", { class: "task-reason" }, status.reason) : null,
      task.last_chat_id && status.kind !== "running"
        ? h("a", { href: `#/c/${task.last_chat_id}`, "aria-label": `Open chat: ${task.name}` }, "Open chat")
        : null,
    ),
    next ? h("p", { class: "task-next" }, next) : null,
    h(
      "div",
      { class: "task-actions" },
      button(task, "run", "Run now"),
      button(task, "edit", "Edit"),
      button(task, "delete", "Delete"),
    ),
  );
}

/** Replaces the cards. `byId` maps model ids to models, `projectById` finds a project. */
export function renderTasks(list, empty, { tasks, byId, projectById, now = Date.now() }) {
  list.replaceChildren(...tasks.map((task) => card(task, { byId, projectById, now })));
  empty.hidden = tasks.length > 0;
}
