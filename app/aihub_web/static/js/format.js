// Pure helpers: how the UI words and groups what the server sends. No DOM here, so Node can test it.

export const LEVEL_LABELS = {
  off: "Off",
  on: "On",
  minimal: "Minimal",
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra high",
  max: "Max",
};

export const levelLabel = (level) => LEVEL_LABELS[level] ?? level;

/** "Google: Gemma 4 31B" becomes "Gemma 4 31B". The picker keeps the full name. */
export const shortName = (name) => String(name).replace(/^[^:]+:\s+/, "");

/** How many of the four bars a level lights, relative to the levels this model offers. */
export function barsFor(level, levels) {
  if (!level || level === "off") return 0;
  const active = levels.filter((l) => l !== "off");
  const at = active.indexOf(level);
  if (at < 0) return 0;
  return Math.ceil(((at + 1) * 4) / active.length);
}

function midnight(now, daysAgo) {
  const d = new Date(now);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate() - daysAgo).getTime();
}

/** Newest-first chats grouped the way people remember them. Empty groups are left out. */
export function groupChats(chats, now = Date.now()) {
  const bounds = [
    ["Today", midnight(now, 0)],
    ["Yesterday", midnight(now, 1)],
    ["Previous 7 days", midnight(now, 7)],
    ["Previous 30 days", midnight(now, 30)],
  ];
  const groups = new Map();
  for (const chat of chats) {
    const label = bounds.find(([, from]) => chat.updated_at >= from)?.[0] ?? "Older";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(chat);
  }
  const order = [...bounds.map(([label]) => label), "Older"];
  return order.filter((label) => groups.has(label)).map((label) => ({ label, items: groups.get(label) }));
}

/** About how many tokens a text takes: the server's rule (three characters to a token, counted in code points). */
export function estimateTokens(text) {
  return Math.floor([...text].length / 3) + 1;
}

/** Whole days until the retention clock deletes a chat, never below zero. */
export function daysLeft(updatedAt, retentionDays, now = Date.now()) {
  return Math.max(0, Math.ceil((updatedAt + retentionDays * 86_400_000 - now) / 86_400_000));
}

export function formatContext(tokens) {
  if (!tokens) return "";
  if (tokens >= 1_900_000) return `${Math.round(tokens / 1_000_000)}M`;
  if (tokens >= 900_000) return "1M";
  return `${Math.round(tokens / 1024)}K`;
}

export function formatCost(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return "";
  if (n === 0) return "$0";
  const decimals = Math.min(8, Math.max(2, Math.ceil(-Math.log10(n)) + 1));
  return `$${n.toFixed(decimals)}`;
}

export function formatDuration(ms) {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 10_000) return `${(ms / 1000).toFixed(1)} s`;
  if (ms < 60_000) return `${Math.round(ms / 1000)} s`;
  return `${Math.floor(ms / 60_000)} min ${Math.round((ms % 60_000) / 1000)} s`;
}

export function formatSpeed(tps) {
  return `${tps < 10 ? tps.toFixed(1) : Math.round(tps)} tok/s`;
}

export function formatBytes(bytes) {
  return `${(bytes / 1e9).toFixed(1)} GB`;
}

export function formatPrice(price) {
  if (!price) return "";
  const money = (v) => {
    const n = Number(v);
    return `$${n >= 0.01 ? (Number.isInteger(n) ? n : n.toFixed(2)) : n.toPrecision(2)}`;
  };
  return `${money(price.in)} in, ${money(price.out)} out`;
}

export const formatPriceLong = (price) => `${formatPrice(price)} per million tokens`;

const shortDate = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });

export function formatDay(iso) {
  return shortDate.format(new Date(`${iso}T12:00:00`));
}

export const TIER_LABELS = { free: "Free", local: "Local", paid: "Paid" };

export const SHAPES = { square: "Square", wide: "Wide", tall: "Tall" };

/** How long one image takes in this shape, as the model's settings estimate it: "Square, about 23 s". */
export function shapeNote(model, shape) {
  const seconds = model?.est_s?.[shape];
  return seconds ? `${SHAPES[shape]}, about ${seconds} s` : SHAPES[shape];
}

/** The sentence that says where the next prompt goes. */
export function routeFor(model) {
  if (!model) {
    return {
      title: "No model available",
      text: "Start Ollama, or set an OpenRouter key in ~/.config/aihub/aihub.env, then restart aihub.",
    };
  }
  if (model.output === "image") {
    return {
      title: `Image mode on ${shortName(model.name)}`,
      text: "Your prompt makes one image on this PC. Nothing leaves the machine; local chat models pause while it works.",
    };
  }
  const title = `New chat on ${shortName(model.name)}`;
  const until = model.expires ? ` Free until ${formatDay(model.expires)}.` : "";
  if (model.tier === "local") {
    return { title, text: "Runs on this PC through Ollama. Nothing leaves the machine." };
  }
  if (model.tier === "free") {
    return {
      title,
      text: `Free model via OpenRouter. OpenRouter and the host serving it may keep what you send, so leave private things to a local model.${until}`,
    };
  }
  if (model.provider === "anthropic") {
    return { title, text: `Billed to your Claude API credits at ${formatPriceLong(model.price)}.` };
  }
  const price = model.price ? ` at ${formatPriceLong(model.price)}` : "";
  return {
    title,
    text: `Paid model via OpenRouter${price}. Requests go only to hosts that say they don't keep prompts.`,
  };
}

/** Keeps the effort if the model offers it, else falls back to the model's own default. */
export function coerceEffort(model, effort) {
  if (!model?.efforts) return null;
  return model.efforts.levels.includes(effort) ? effort : model.efforts.default;
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** A moment as a person says it: "today 08:05", "tomorrow 08:00", "Thu 07:00" within the week, else "27 Oct 07:00". */
export function formatWhen(ms, now = Date.now()) {
  const at = new Date(ms);
  const today = new Date(now);
  const midnight = (date) => new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((midnight(at) - midnight(today)) / 86_400_000);
  const time = `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
  if (days === 0) return `today ${time}`;
  if (days === 1) return `tomorrow ${time}`;
  if (days === -1) return `yesterday ${time}`;
  if (days > 1 && days < 7) return `${WEEKDAYS[at.getDay()]} ${time}`;
  const year = at.getFullYear() === today.getFullYear() ? "" : ` ${at.getFullYear()}`;
  return `${at.getDate()} ${MONTHS[at.getMonth()]}${year} ${time}`;
}

/** What a task's card says about its last run: its kind (for the colour), a label, and the reason if there is one. */
export function taskStatus(task, now = Date.now()) {
  if (task.running) return { kind: "running", label: "Running now" };
  if (task.switched_off) return { kind: "off", label: "Switched off", reason: task.last_error };
  const when = formatWhen(task.last_run_at, now);
  switch (task.last_status) {
    case "done":
      return { kind: "done", label: `Done ${when}` };
    case "error":
      return { kind: "error", label: `Failed ${when}`, reason: task.last_error };
    case "stopped":
      return { kind: "error", label: `Stopped ${when}`, reason: task.last_error };
    case "skipped":
      return { kind: "skipped", label: `Skipped ${when}`, reason: task.last_error };
    default:
      return { kind: "none", label: "Not run yet" };
  }
}

/** The line about the next run: null for a task that is off. A run that is due starts within seconds. */
export function taskNext(task, now = Date.now()) {
  if (!task.enabled || task.next_run_at == null) return null;
  return task.next_run_at <= now ? "Due now" : `Next run ${formatWhen(task.next_run_at, now)}`;
}
