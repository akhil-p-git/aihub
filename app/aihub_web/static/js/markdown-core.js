// The parts of markdown rendering that need no DOM: which blocks to redo, and which language a fence names.

// Highlighted lazily, one file per language. Keep in step with LANGUAGES in scripts/vendor_frontend.py.
export const LANGUAGES = new Set(
  "bash c cpp csharp css diff dockerfile go ini java javascript json kotlin lua makefile markdown php plaintext powershell python ruby rust scss shell sql swift typescript xml yaml".split(
    " ",
  ),
);

const ALIASES = {
  js: "javascript",
  jsx: "javascript",
  mjs: "javascript",
  node: "javascript",
  ts: "typescript",
  tsx: "typescript",
  py: "python",
  python3: "python",
  sh: "bash",
  zsh: "bash",
  console: "shell",
  shellsession: "shell",
  html: "xml",
  svg: "xml",
  xhtml: "xml",
  yml: "yaml",
  toml: "ini",
  conf: "ini",
  "c++": "cpp",
  h: "c",
  cs: "csharp",
  "c#": "csharp",
  rs: "rust",
  golang: "go",
  rb: "ruby",
  kt: "kotlin",
  md: "markdown",
  docker: "dockerfile",
  ps1: "powershell",
  pwsh: "powershell",
  text: "plaintext",
  txt: "plaintext",
  plain: "plaintext",
};

/** The vendored language a fence's info string asks for, or null. */
export function resolveLanguage(info) {
  const word = String(info ?? "")
    .trim()
    .split(/\s+/)[0]
    .toLowerCase()
    .replace(/^language-/, "");
  const name = ALIASES[word] ?? word;
  return LANGUAGES.has(name) ? name : null;
}

/** Index of the first block whose source text changed, so everything before it can be kept as is. */
export function firstChange(previous, next) {
  let i = 0;
  while (i < previous.length && i < next.length && previous[i] === next[i]) i++;
  return i;
}

/** False while a fenced code block is still waiting for its closing fence. */
export function hasClosingFence(raw) {
  const open = /^ {0,3}(`{3,}|~{3,})/.exec(raw);
  if (!open) return true;
  const lines = raw.replace(/\s+$/, "").split("\n");
  if (lines.length < 2) return false;
  const last = lines[lines.length - 1].trim();
  return last.length >= open[1].length && [...last].every((c) => c === open[1][0]);
}
