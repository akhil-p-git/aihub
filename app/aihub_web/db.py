"""SQLite storage for chats: one local file, and chats expire after the retention period."""

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
DAY_MS = 86_400_000
# Columns update_chat may change; the names are interpolated into SQL, so never take them from input.
EDITABLE_CHAT_COLUMNS = ("title", "model", "effort")

SCHEMA = """
CREATE TABLE chats (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    model      TEXT NOT NULL,
    effort     TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX chats_updated ON chats (updated_at DESC);

CREATE TABLE messages (
    id         TEXT PRIMARY KEY,
    chat_id    TEXT NOT NULL REFERENCES chats (id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    thinking   TEXT,
    model      TEXT,
    effort     TEXT,
    usage      TEXT,
    status     TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('done', 'stopped', 'error')),
    error      TEXT,
    created_at INTEGER NOT NULL,
    UNIQUE (chat_id, seq)
);

CREATE TABLE settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


@dataclass(frozen=True, slots=True)
class Chat:
    id: str
    title: str
    model: str
    effort: str | None
    created_at: int
    updated_at: int
    snippet: str | None = None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Message:
    id: str
    chat_id: str
    seq: int
    role: str
    content: str
    thinking: str | None
    model: str | None
    effort: str | None
    usage: dict[str, object] | None
    status: str
    error: dict[str, str] | None
    created_at: int

    def to_json(self) -> dict[str, object]:
        return asdict(self)


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(text: str, term: str, width: int = 60) -> str:
    """The words around the first match, with an ellipsis where the text was cut."""
    at = text.lower().find(term.lower())
    if at < 0:
        return " ".join(text[: 2 * width].split())
    start, end = max(0, at - width), at + len(term) + width
    body = " ".join(re.sub(r"`{3,}[\w+#.-]*|`", "", text[start:end]).split())
    return ("…" if start else "") + body + ("…" if end < len(text) else "")


def _message(row: sqlite3.Row) -> Message:
    return Message(
        id=row["id"],
        chat_id=row["chat_id"],
        seq=row["seq"],
        role=row["role"],
        content=row["content"],
        thinking=row["thinking"],
        model=row["model"],
        effort=row["effort"],
        usage=json.loads(row["usage"]) if row["usage"] else None,
        status=row["status"],
        error=json.loads(row["error"]) if row["error"] else None,
        created_at=row["created_at"],
    )


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Create the file ourselves so it is private from the first byte; SQLite gives -wal and -shm the same mode.
        os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        for pragma in (
            "journal_mode = WAL",
            "synchronous = NORMAL",
            "foreign_keys = ON",
            # Deleted chats are overwritten with zeroes instead of lingering in free pages.
            "secure_delete = ON",
        ):
            self._db.execute(f"PRAGMA {pragma}")
        self._migrate()

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than this app ({SCHEMA_VERSION})"
            )
        if version == 0:
            with self._lock:
                self._db.executescript(
                    f"BEGIN;\n{SCHEMA}\nPRAGMA user_version = {SCHEMA_VERSION};\nCOMMIT;"
                )

    def _now(self) -> int:
        return int(self._clock() * 1000)

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.close()

    # chats

    def create_chat(self, title: str, model: str, effort: str | None) -> Chat:
        now = self._now()
        chat = Chat(secrets.token_hex(8), title, model, effort, now, now)
        with self._tx() as db:
            db.execute(
                "INSERT INTO chats (id, title, model, effort, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (chat.id, chat.title, chat.model, chat.effort, now, now),
            )
        return chat

    def get_chat(self, chat_id: str) -> Chat | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM chats WHERE id = ?", (chat_id,)
            ).fetchone()
        return Chat(**dict(row)) if row else None

    def list_chats(self, query: str | None = None, limit: int = 200) -> list[Chat]:
        """Newest activity first. A query matches titles and message text, and adds a snippet."""
        term = (query or "").strip()
        with self._lock:
            if not term:
                rows = self._db.execute(
                    "SELECT * FROM chats ORDER BY updated_at DESC LIMIT ?", (limit,)
                ).fetchall()
                return [Chat(**dict(r)) for r in rows]
            rows = self._db.execute(
                """
                SELECT c.*, (
                    SELECT m.content FROM messages m
                    WHERE m.chat_id = c.id AND m.content LIKE ? ESCAPE '\\'
                    ORDER BY m.seq LIMIT 1
                ) AS hit
                FROM chats c
                WHERE c.title LIKE ? ESCAPE '\\' OR hit IS NOT NULL
                ORDER BY c.updated_at DESC LIMIT ?
                """,
                (_like(term), _like(term), limit),
            ).fetchall()
        chats = []
        for row in rows:
            data = dict(row)
            hit = data.pop("hit")
            chats.append(Chat(**data, snippet=_snippet(hit, term) if hit else None))
        return chats

    def update_chat(
        self, chat_id: str, changes: Mapping[str, str | None]
    ) -> Chat | None:
        """Edit title, model or effort. This is not activity: the retention clock keeps running."""
        columns = [c for c in EDITABLE_CHAT_COLUMNS if c in changes]
        if columns:
            assignments = ", ".join(f"{c} = ?" for c in columns)
            with self._tx() as db:
                db.execute(
                    f"UPDATE chats SET {assignments} WHERE id = ?",
                    [*(changes[c] for c in columns), chat_id],
                )
        return self.get_chat(chat_id)

    def delete_chat(self, chat_id: str) -> bool:
        with self._tx() as db:
            return db.execute("DELETE FROM chats WHERE id = ?", (chat_id,)).rowcount > 0

    def delete_all(self) -> int:
        with self._tx() as db:
            count = db.execute("DELETE FROM chats").rowcount
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return count

    def purge_expired(self, retention_days: int) -> int:
        """Delete chats whose last message is older than the retention period."""
        cutoff = self._now() - retention_days * DAY_MS
        with self._tx() as db:
            count = db.execute(
                "DELETE FROM chats WHERE updated_at < ?", (cutoff,)
            ).rowcount
        if count:
            # Move the deleted text out of the write-ahead log too.
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            log.info("purged %d chat(s) older than %d days", count, retention_days)
        return count

    # messages

    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        *,
        thinking: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        usage: dict[str, object] | None = None,
        status: str = "done",
        error: dict[str, str] | None = None,
    ) -> Message:
        now = self._now()
        with self._tx() as db:
            seq = db.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()[0]
            message = Message(
                secrets.token_hex(8),
                chat_id,
                seq,
                role,
                content,
                thinking,
                model,
                effort,
                usage,
                status,
                error,
                now,
            )
            db.execute(
                "INSERT INTO messages (id, chat_id, seq, role, content, thinking, model, effort, usage, status, error, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message.id,
                    chat_id,
                    seq,
                    role,
                    content,
                    thinking,
                    model,
                    effort,
                    json.dumps(usage) if usage else None,
                    status,
                    json.dumps(error) if error else None,
                    now,
                ),
            )
            db.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
        return message

    def messages(self, chat_id: str) -> list[Message]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM messages WHERE chat_id = ? ORDER BY seq", (chat_id,)
            ).fetchall()
        return [_message(r) for r in rows]

    def delete_message(self, message_id: str) -> None:
        with self._tx() as db:
            db.execute("DELETE FROM messages WHERE id = ?", (message_id,))

    # settings

    def get_setting(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM settings WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as db:
            db.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
