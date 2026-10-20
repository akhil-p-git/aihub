// The chat list: recent chats grouped by day, or search results.

import { h, icon } from "./dom.js";
import { groupChats } from "./format.js";

function row(chat, current) {
  return h(
    "div",
    { class: "chat-row", "data-id": chat.id },
    h(
      "a",
      { class: "chat-link", href: `#/c/${chat.id}`, "aria-current": chat.id === current ? "page" : null },
      h("span", { class: "t" }, chat.title),
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

export function renderSidebar(nav, { chats, current, query }) {
  nav.setAttribute("aria-busy", "false");
  if (!chats.length) {
    const text = query ? `Nothing matches “${query}”.` : "No chats yet. Start one and it will show up here.";
    nav.replaceChildren(h("p", { class: "empty-list" }, text));
    return;
  }
  const groups = query ? [{ label: "Results", items: chats }] : groupChats(chats);
  nav.replaceChildren(
    ...groups.map(({ label, items }) =>
      h(
        "section",
        { "aria-label": label },
        h("p", { class: "group-label" }, label),
        items.map((chat) => row(chat, current)),
      ),
    ),
  );
}
