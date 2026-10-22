// The chat list: projects with their chats, then the other chats grouped by day, or search results.

import { h, icon } from "./dom.js";
import { groupChats } from "./format.js";

/** The clock before the title of a chat that a scheduled task made, with the words for a screen reader. */
const taskMark = () =>
  h("span", { class: "task-mark" }, icon("clock"), h("span", { class: "sr-only" }, "Scheduled task: "));

function row(chat, current) {
  return h(
    "div",
    { class: "chat-row", "data-id": chat.id },
    h(
      "a",
      { class: "chat-link", href: `#/c/${chat.id}`, "aria-current": chat.id === current ? "page" : null },
      h("span", { class: "t" }, chat.task_id ? taskMark() : null, chat.title),
      chat.snippet ? h("span", { class: "snip" }, chat.snippet) : null,
    ),
    h(
      "button",
      {
        class: "menu-btn",
        type: "button",
        "aria-label": `Options for ${chat.title}`,
        "aria-haspopup": "menu",
        "aria-expanded": "false",
        "data-menu": chat.id,
      },
      icon("more"),
    ),
  );
}

function projectRow(project, chats, current, isOpen) {
  const listId = `project-chats-${project.id}`;
  const n = chats.length;
  return h(
    "div",
    { class: "project", "data-id": project.id },
    h(
      "div",
      { class: "project-head" },
      h(
        "button",
        {
          class: "project-toggle",
          type: "button",
          "aria-expanded": String(isOpen),
          "aria-controls": listId,
          "data-toggle": project.id,
        },
        icon("chevron"),
        h("span", { class: "t" }, project.name),
        h("span", { class: "count", "aria-hidden": "true" }, String(n)),
        h("span", { class: "sr-only" }, `, ${n} ${n === 1 ? "chat" : "chats"}`),
      ),
      h(
        "button",
        {
          class: "menu-btn project-new",
          type: "button",
          "aria-label": `New chat in ${project.name}`,
          "data-new-in": project.id,
        },
        icon("plus"),
      ),
      h(
        "button",
        {
          class: "menu-btn",
          type: "button",
          "aria-label": `Options for ${project.name}`,
          "aria-haspopup": "menu",
          "aria-expanded": "false",
          "data-project-menu": project.id,
        },
        icon("more"),
      ),
    ),
    h(
      "div",
      { class: "project-chats", id: listId, hidden: !isOpen },
      chats.map((chat) => row(chat, current)),
    ),
  );
}

function projectsSection(projects, chats, current, open) {
  return h(
    "section",
    { "aria-label": "Projects" },
    h(
      "div",
      { class: "group-row" },
      h("p", { class: "group-label" }, "Projects"),
      h("button", { class: "icon-btn", type: "button", id: "new-project", "aria-label": "New project" }, icon("plus")),
    ),
    projects.length
      ? projects.map((project) =>
          projectRow(
            project,
            chats.filter((chat) => chat.project_id === project.id),
            current,
            open.has(project.id),
          ),
        )
      : h("p", { class: "empty-list" }, "Group chats that share instructions."),
  );
}

export function renderSidebar(nav, { chats, projects = [], current, query, open = new Set() }) {
  nav.setAttribute("aria-busy", "false");
  if (query) {
    nav.replaceChildren(
      chats.length
        ? h(
            "section",
            { "aria-label": "Results" },
            h("p", { class: "group-label" }, "Results"),
            chats.map((chat) => row(chat, current)),
          )
        : h("p", { class: "empty-list" }, `Nothing matches “${query}”.`),
    );
    return;
  }
  // A chat whose project this page does not know (deleted elsewhere) is listed with the others.
  const known = new Set(projects.map((project) => project.id));
  const loose = chats.filter((chat) => !known.has(chat.project_id));
  nav.replaceChildren(
    projectsSection(projects, chats, current, open),
    ...groupChats(loose).map(({ label, items }) =>
      h(
        "section",
        { "aria-label": label },
        h("p", { class: "group-label" }, label),
        items.map((chat) => row(chat, current)),
      ),
    ),
    ...(chats.length || projects.length
      ? []
      : [h("p", { class: "empty-list" }, "No chats yet. Start one and it will show up here.")]),
  );
}
