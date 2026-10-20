#!/usr/bin/env python3
"""Seed a state directory with one long chat, for render-cost tests. Usage: seed.py STATE_DIR TURNS"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
from aihub_web.db import Store

REPLY = """Here is a short answer with **bold** text and `inline code`.

```python
def square(n):
    return n * n
```

- one
- two
- three
"""

state, turns = Path(sys.argv[1]), int(sys.argv[2])
store = Store(state / "chats.db")
chat = store.create_chat("A long conversation", "ollama/qwen3:8b", "off")
for i in range(1, turns + 1):
    store.add_message(chat.id, "user", f"Question {i}: what is {i} squared?")
    store.add_message(
        chat.id,
        "assistant",
        f"{i} squared is {i * i}.\n\n{REPLY}",
        model="ollama/qwen3:8b",
        effort="off",
        usage={"out": 60, "tps": 50.0, "total_ms": 1200},
    )
store.close()
print(chat.id)
