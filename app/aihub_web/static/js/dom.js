// Tiny DOM helpers and the icon set. Everything here builds nodes; nothing parses model text as HTML.

const SVG = "http://www.w3.org/2000/svg";

const ICONS = {
  menu: [["path", { d: "M3 6h14M3 10h14M3 14h14" }]],
  plus: [["path", { d: "M10 4v12M4 10h12" }]],
  panel: [
    ["rect", { x: 3, y: 4, width: 14, height: 12, rx: 2 }],
    ["path", { d: "M8 4v12" }],
  ],
  sliders: [
    ["path", { d: "M3 6h8M15 6h2M3 14h2M9 14h8" }],
    ["circle", { cx: 13, cy: 6, r: 2 }],
    ["circle", { cx: 7, cy: 14, r: 2 }],
  ],
  check: [["path", { d: "M4.5 10.5l3.5 3.5 7.5-8.5" }]],
  copy: [
    ["rect", { x: 7, y: 7, width: 9, height: 9, rx: 2 }],
    ["path", { d: "M13 7V5.5A1.5 1.5 0 0 0 11.5 4h-6A1.5 1.5 0 0 0 4 5.5v6A1.5 1.5 0 0 0 5.5 13H7" }],
  ],
  retry: [
    ["path", { d: "M16 10a6 6 0 1 1-1.8-4.3" }],
    ["path", { d: "M16 3.5v3.2h-3.2" }],
  ],
  up: [["path", { d: "M10 16V4M5 9l5-5 5 5" }]],
  stop: [["rect", { class: "solid", x: 5.5, y: 5.5, width: 9, height: 9, rx: 2 }]],
  more: [
    ["circle", { class: "solid", cx: 4.5, cy: 10, r: 1.4 }],
    ["circle", { class: "solid", cx: 10, cy: 10, r: 1.4 }],
    ["circle", { class: "solid", cx: 15.5, cy: 10, r: 1.4 }],
  ],
  x: [["path", { d: "M5 5l10 10M15 5L5 15" }]],
  trash: [["path", { d: "M4 6h12M8 6V4h4v2M6 6l.8 10h6.4L14 6" }]],
  pencil: [["path", { d: "M4 16l1-4 8-8 3 3-8 8-4 1z" }]],
  down: [["path", { d: "M10 4v12M5 11l5 5 5-5" }]],
  chevron: [["path", { d: "M8 5l5 5-5 5" }]],
  folder: [
    [
      "path",
      {
        d: "M3 6.5A1.5 1.5 0 0 1 4.5 5H8l1.7 2h5.8A1.5 1.5 0 0 1 17 8.5v6a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 3 14.5z",
      },
    ],
  ],
  alert: [
    ["circle", { cx: 10, cy: 10, r: 7.5 }],
    ["path", { d: "M10 6v4.5M10 13.5v.01" }],
  ],
};

function shape([tag, attributes]) {
  const el = document.createElementNS(SVG, tag);
  for (const [name, value] of Object.entries(attributes)) el.setAttribute(name, value);
  return el;
}

/** An element with attributes, listeners (onClick) and children. null and false children are skipped. */
export function h(tag, props = {}, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value == null || value === false) continue;
    if (key.startsWith("on")) el.addEventListener(key.slice(2).toLowerCase(), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
  el.append(...children.flat(Infinity).filter((child) => child != null && child !== false));
  return el;
}

export function icon(name) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  svg.append(...ICONS[name].map(shape));
  return svg;
}

const BARS = [
  [2, 12, 6],
  [6.5, 9, 9],
  [11, 6, 12],
  [15.5, 3, 15],
];

/** Four rising bars with the first `lit` filled: an intensity level, or (animated by CSS) a model at work. */
export function bars(lit = 0) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 20 20");
  svg.setAttribute("class", "bars");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  BARS.forEach(([x, y, height], i) => {
    const bar = document.createElementNS(SVG, "rect");
    bar.setAttribute("x", x);
    bar.setAttribute("y", y);
    bar.setAttribute("width", 3);
    bar.setAttribute("height", height);
    bar.setAttribute("rx", 1);
    if (i < lit) bar.dataset.on = "";
    bar.style.setProperty("--i", i);
    svg.append(bar);
  });
  return svg;
}

export function working(label) {
  return h("div", { class: "working" }, bars(4), h("span", {}, label));
}

/** Puts the popover above (or below) its anchor, clamped to the viewport. Call after showPopover(). */
export function placePopover(pop, anchor, { below = false } = {}) {
  const rect = anchor.getBoundingClientRect();
  const left = Math.max(8, Math.min(rect.left, window.innerWidth - pop.offsetWidth - 8));
  pop.style.setProperty("--pop-x", `${left}px`);
  if (below) {
    pop.style.setProperty("--pop-top", `${rect.bottom + 4}px`);
  } else {
    pop.style.setProperty("--pop-bottom", `${window.innerHeight - rect.top + 8}px`);
  }
}
