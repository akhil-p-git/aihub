// Renders a reply as it streams. Only the block still growing is redone, so the cost per frame stays flat.
// Output is sanitised twice over: raw HTML is allowlisted by DOMPurify, and the CSP blocks scripts and remote images.

import { Marked } from "/lib/marked-18.1.0/marked.esm.js";
import DOMPurify from "/lib/dompurify-3.4.16/purify.es.mjs";
import { h, icon } from "./dom.js";
import { hasClosingFence, resolveLanguage } from "./markdown-core.js";

const HIGHLIGHT = "/lib/highlight-11.12.0";

const escapeHtml = (text) =>
  text.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

// Raw HTML that could only ever be an attack on a chat page is shown as text instead of being parsed.
const ACTIVE_HTML =
  /^\s*<\/?(script|style|iframe|frame|frameset|object|embed|link|meta|base|form|svg|math|img|template|noscript)\b/i;

const parser = new Marked({
  gfm: true,
  breaks: false,
  renderer: {
    // A remote image in a reply could carry the chat out in its URL, so it becomes a plain link.
    image: ({ href, text }) => `<a href="${escapeHtml(href)}">${escapeHtml(text || "image")}</a>`,
    html: ({ text, block }) => {
      if (!ACTIVE_HTML.test(text)) return text;
      return block ? `<p>${escapeHtml(text.trim())}</p>` : escapeHtml(text);
    },
  },
});

const SANITIZE = {
  ALLOWED_TAGS: [
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "details",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "i",
    "input",
    "kbd",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "strong",
    "sub",
    "summary",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
  ],
  ALLOWED_ATTR: [
    "href",
    "title",
    "colspan",
    "rowspan",
    "align",
    "start",
    "type",
    "checked",
    "disabled",
    "open",
    "class",
  ],
  ALLOWED_URI_REGEXP: /^(?:https?:|mailto:)/i,
  ALLOW_DATA_ATTR: false,
  ALLOW_ARIA_ATTR: false,
  RETURN_DOM_FRAGMENT: true,
};

// A class may only name a code language; anything else could borrow the UI's own styles or handlers.
DOMPurify.addHook("uponSanitizeAttribute", (node, data) => {
  if (data.attrName === "class") {
    data.keepAttr = node.tagName === "CODE" && /^language-[\w+#.-]{1,30}$/.test(data.attrValue);
  }
});
DOMPurify.addHook("afterSanitizeAttributes", (node) => {
  if (node.tagName === "A" && node.hasAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer nofollow");
  }
});

// A second, separate sanitiser for highlight.js output: spans with hljs classes, nothing else.
const spans = DOMPurify(window);
spans.addHook("uponSanitizeAttribute", (node, data) => {
  if (data.attrName === "class") data.keepAttr = /^hljs(-[\w-]+)?( hljs(-[\w-]+)?)*$/.test(data.attrValue);
});
const SPANS = { ALLOWED_TAGS: ["span"], ALLOWED_ATTR: ["class"], RETURN_DOM_FRAGMENT: true };

let highlighter;
const languages = new Map();

function loadHighlighter() {
  highlighter ??= import(`${HIGHLIGHT}/core.min.js`).then((m) => m.default);
  return highlighter;
}

function loadLanguage(hljs, name) {
  if (!languages.has(name)) {
    languages.set(
      name,
      import(`${HIGHLIGHT}/languages/${name}.min.js`).then((m) => hljs.registerLanguage(name, m.default)),
    );
  }
  return languages.get(name);
}

async function highlightCode(code) {
  const name = resolveLanguage(code.className);
  if (!name || code.dataset.hl) return;
  code.dataset.hl = "1";
  const source = code.textContent;
  try {
    const hljs = await loadHighlighter();
    await loadLanguage(hljs, name);
    // The block may have been redone while the language loaded.
    if (code.isConnected && code.textContent === source) {
      const { value } = hljs.highlight(source, { language: name, ignoreIllegals: true });
      code.replaceChildren(spans.sanitize(value, SPANS));
    }
  } catch {
    // An unhighlighted block is still readable.
  }
}

// A code block or table that scrolls sideways has to be reachable by keyboard, or its overflow can't be read.
// A ResizeObserver also fires once when it starts watching, which is the first check.
const scrollers = new ResizeObserver((entries) => {
  const scrolls = entries.map(({ target }) => target.scrollWidth > target.clientWidth); // reads, then writes
  entries.forEach(({ target }, i) => {
    if (!target.isConnected) scrollers.unobserve(target);
    else if (scrolls[i]) {
      target.tabIndex = 0;
      target.setAttribute("role", "group");
      target.setAttribute("aria-label", target.matches("pre") ? "Code, scrolls sideways" : "Table, scrolls sideways");
    } else {
      for (const name of ["tabindex", "role", "aria-label"]) target.removeAttribute(name);
    }
  });
});

function decorate(fragment) {
  for (const input of fragment.querySelectorAll("input")) {
    if (input.getAttribute("type") === "checkbox") input.disabled = true;
    else input.remove();
  }
  for (const table of fragment.querySelectorAll("table")) {
    const wrap = h("div", { class: "table-wrap" });
    table.replaceWith(wrap);
    wrap.append(table);
  }
  for (const pre of fragment.querySelectorAll("pre")) {
    const info = pre.querySelector("code")?.className.replace(/^language-/, "") ?? "";
    const copy = h("button", { class: "btn", type: "button", "data-copy": true }, icon("copy"), h("span", {}, "Copy"));
    const box = h("div", { class: "code" }, h("div", { class: "code-head" }, h("span", {}, info || "Code"), copy));
    pre.replaceWith(box);
    box.append(pre);
  }
}

function renderBlock(token, links) {
  const tokens = [token];
  tokens.links = links;
  const fragment = DOMPurify.sanitize(parser.parser(tokens), SANITIZE);
  decorate(fragment);
  const wrap = h("div", { class: "blk" });
  wrap.append(fragment);
  return wrap;
}

/** A renderer bound to one container. update(text) can be called with a longer text as often as you like. */
export function createRenderer(container) {
  const blocks = []; // { raw, el }

  return {
    update(text, final = false) {
      const tokens = parser.lexer(text);
      let keep = 0;
      while (keep < blocks.length && keep < tokens.length && blocks[keep].raw === tokens[keep].raw) keep++;
      while (blocks.length > keep) blocks.pop().el?.remove();
      for (let i = keep; i < tokens.length; i++) {
        const el = tokens[i].type === "space" ? null : renderBlock(tokens[i], tokens.links);
        if (el) container.append(el);
        blocks.push({ raw: tokens[i].raw, el });
      }
      if (final) container.querySelectorAll(".code pre, .table-wrap").forEach((el) => scrollers.observe(el));
      // Highlight only code that is finished: a half-streamed block would be tokenised wrongly.
      blocks.forEach((block, i) => {
        const finished = final || i < blocks.length - 1 || hasClosingFence(block.raw);
        if (block.el && finished) block.el.querySelectorAll(".code code").forEach(highlightCode);
      });
    },
  };
}

export function renderMarkdown(container, text) {
  createRenderer(container).update(text, true);
}
