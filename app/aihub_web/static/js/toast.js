import { h } from "./dom.js";

const host = document.getElementById("toasts");

/** A short message at the bottom of the screen. An action adds one button (for Undo, say). */
export function toast(message, { action, timeout = 5000 } = {}) {
  const el = h("div", { class: "toast" }, h("span", {}, message));
  const close = () => {
    clearTimeout(timer);
    el.remove();
  };
  if (action) {
    el.append(
      h(
        "button",
        {
          class: "btn",
          type: "button",
          onClick: () => {
            close();
            action.run();
          },
        },
        action.label,
      ),
    );
  }
  const timer = setTimeout(close, timeout);
  host.append(el);
  return close;
}
