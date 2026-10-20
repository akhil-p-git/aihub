// Talking to the server: JSON calls and the newline-delimited stream of a reply.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

const OFFLINE = "Can't reach the aihub server. Is the service running?";

async function toError(response) {
  try {
    const { error } = await response.json();
    return new ApiError(response.status, error.code, error.message);
  } catch {
    return new ApiError(response.status, "http", `The server answered ${response.status}.`);
  }
}

async function request(method, url, body) {
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, "offline", OFFLINE);
  }
  if (!response.ok) throw await toError(response);
  return response.status === 204 ? null : response.json();
}

export const api = {
  get: (url) => request("GET", url),
  post: (url, body) => request("POST", url, body),
  patch: (url, body) => request("PATCH", url, body),
  put: (url, body) => request("PUT", url, body),
  del: (url, body) => request("DELETE", url, body),
};

/** Parses one JSON object per line from a reader, whatever way the network cut the chunks. */
export async function* readEvents(reader) {
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    buffer += value ? decoder.decode(value, { stream: true }) : "";
    if (done) buffer += decoder.decode();
    for (let at = buffer.indexOf("\n"); at >= 0; at = buffer.indexOf("\n")) {
      const line = buffer.slice(0, at);
      buffer = buffer.slice(at + 1);
      if (line.trim()) yield JSON.parse(line);
    }
    if (done) break;
  }
  if (buffer.trim()) yield JSON.parse(buffer);
}

/** Streams a reply. Throws ApiError before any event if the request was refused. */
export async function* streamChat(body, signal) {
  let response;
  try {
    response = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body),
      signal,
    });
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError(0, "offline", OFFLINE);
  }
  if (!response.ok) throw await toError(response);
  try {
    yield* readEvents(response.body.getReader());
  } catch (err) {
    if (err.name === "AbortError") throw err;
    throw new ApiError(0, "interrupted", "The connection dropped while the reply was streaming.");
  }
}
