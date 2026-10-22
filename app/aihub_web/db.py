"""SQLite storage for chats: one local file, and chats expire after the retention period."""

import json
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
# Set on the copies this app makes before a schema upgrade (SQLite's application_id, "AHB1"), so that expiring
# old copies never touches a file somebody else put in the folder under a similar name.
BACKUP_APPLICATION_ID = 0x41484231
# Columns update_chat may change; the names are interpolated into SQL, so never take them from input.
EDITABLE_CHAT_COLUMNS = ("title", "model", "effort", "project_id")

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

# Added in version 2. The pixels live in the row, so a reply's images are deleted with the reply, with the
# chat and with the retention sweep, and secure_delete wipes them like text.
IMAGES_SCHEMA = """
CREATE TABLE images (
    id         TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    chat_id    TEXT NOT NULL,
    mime       TEXT NOT NULL CHECK (mime = 'image/webp'),
    width      INTEGER NOT NULL,
    height     INTEGER NOT NULL,
    seed       INTEGER NOT NULL,
    steps      INTEGER NOT NULL,
    model      TEXT NOT NULL,
    ref        TEXT,
    bytes      INTEGER NOT NULL,
    data       BLOB NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX images_message ON images (message_id);
CREATE INDEX images_chat ON images (chat_id);
"""

# Added in version 3. A project is configuration, not chat content, so it is never expired; deleting one sets its
# chats' project_id to NULL (delete_project can remove the chats first).
PROJECTS_SCHEMA = """
CREATE TABLE projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '',
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL
);
ALTER TABLE chats ADD COLUMN project_id TEXT REFERENCES projects (id) ON DELETE SET NULL;
CREATE INDEX chats_project ON chats (project_id);
"""

# Each step takes a database from the version before it to its own, and step 1 is the base schema. A new file runs every
# step, so it ends up with exactly the tables, columns and SQL that an upgraded file has.
MIGRATIONS = {1: SCHEMA, 2: IMAGES_SCHEMA, 3: PROJECTS_SCHEMA}
SCHEMA_VERSION = max(MIGRATIONS)


def shred(path: Path) -> None:
    """Overwrite a file with zeroes, then remove it: what secure_delete does for rows, and as best effort as it is
    (a disk that writes copy-on-write may keep the old blocks)."""
    remaining = path.stat().st_size
    with open(path, "r+b") as file:
        while remaining > 0:
            remaining -= file.write(bytes(min(remaining, 1 << 20)))
        file.flush()
        os.fsync(file.fileno())
    path.unlink()


@dataclass(frozen=True, slots=True)
class Chat:
    id: str
    title: str
    model: str
    effort: str | None
    created_at: int
    updated_at: int
    project_id: str | None = None
    snippet: str | None = None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    instructions: str
    created_at: int
    updated_at: int
    chat_count: int = 0

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
    # Metadata of the images in this reply ({id, w, h, seed, steps, model, ref}), never their bytes.
    images: list[dict[str, object]] | None = None

    def to_json(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ImageRow:
    """An image on its way into the database; `ref` is the image an edit started from."""

    width: int
    height: int
    seed: int
    steps: int
    model: str
    data: bytes
    ref: str | None = None


def _like(term: str) -> str:
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(text: str, term: str, width: int = 60) -> str:
    """The words around the first match, with an ellipsis where the text was cut."""
    at = text.lower().find(term.lower())
    if at < 0:
        return " ".join(text[: 2 * width].split())
    start, end = max(0, at - width), at + len(term) + width
    fenceless = re.sub(r"(?m)^ {0,3}`{3,}[\w+#.-]*[ \t]*$", "", text[start:end])
    body = " ".join(fenceless.replace("`", "").split())
    return ("…" if start else "") + body + ("…" if end < len(text) else "")


def _message(row: sqlite3.Row, images: list[dict[str, object]] | None) -> Message:
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
        images=images,
    )


class Store:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._path = path
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
        try:
            self._migrate()
        except BaseException:
            # A step that failed leaves its transaction open; closing rolls it back and frees the database's lock.
            self._db.close()
            raise

    def _migrate(self) -> None:
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {version} is newer than this app ({SCHEMA_VERSION})"
            )
        if 0 < version < SCHEMA_VERSION:
            self._back_up(version)
        for step in range(version + 1, SCHEMA_VERSION + 1):
            self._run_step(step)

    def _back_up(self, version: int) -> None:
        """Copy the file as it is before a schema step rewrites it: the chats live nowhere else. If the copy cannot
        be made the upgrade does not happen. The copy is private, one file, and goes when its chats would have."""
        stem = f"{self._path.name}.v{version}"
        target = self._path.with_name(f"{stem}.bak")
        for n in range(2, 1000):
            if not target.exists():
                break
            target = self._path.with_name(f"{stem}-{n}.bak")
        os.close(os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        try:
            copy = sqlite3.connect(target)
            try:
                self._db.backup(copy)
                copy.execute(f"PRAGMA application_id = {BACKUP_APPLICATION_ID}")
            finally:
                copy.close()
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        stamp = self._clock()
        os.utime(target, (stamp, stamp))
        log.info(
            "copied the database to %s before upgrading it from schema %d",
            target.name,
            version,
        )

    def _backups(self) -> list[Path]:
        """The copies this app made: named like one, and marked as one in the file's header."""
        found = []
        for path in sorted(self._path.parent.glob(f"{self._path.name}.v*.bak")):
            try:
                with open(path, "rb") as file:
                    header = file.read(100)
            except OSError:
                continue
            if header[:16] == b"SQLite format 3\0" and header[
                68:72
            ] == BACKUP_APPLICATION_ID.to_bytes(4, "big"):
                found.append(path)
        return found

    def _run_step(self, step: int) -> None:
        """One schema step in one transaction: a step that fails leaves the file at the version before it."""
        with self._lock:
            self._db.executescript(
                f"BEGIN;\n{MIGRATIONS[step]}\nPRAGMA user_version = {step};\nCOMMIT;"
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

    def _wipe_wal(self) -> None:
        """Move deleted pixels out of the write-ahead log: secure_delete only zeroes pages in the main file."""
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # chats

    def create_chat(
        self,
        title: str,
        model: str,
        effort: str | None,
        project_id: str | None = None,
    ) -> Chat:
        now = self._now()
        chat = Chat(secrets.token_hex(8), title, model, effort, now, now, project_id)
        with self._tx() as db:
            db.execute(
                "INSERT INTO chats (id, title, model, effort, created_at, updated_at, project_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (chat.id, chat.title, chat.model, chat.effort, now, now, project_id),
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
            had_images = db.execute(
                "SELECT EXISTS (SELECT 1 FROM images WHERE chat_id = ?)", (chat_id,)
            ).fetchone()[0]
            deleted = (
                db.execute("DELETE FROM chats WHERE id = ?", (chat_id,)).rowcount > 0
            )
        if had_images:
            self._wipe_wal()
        return deleted

    def delete_all(self) -> int:
        with self._tx() as db:
            count = db.execute("DELETE FROM chats").rowcount
        self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for backup in self._backups():
            shred(backup)
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
        # A copy made before an upgrade holds chats as they were, so it may not outlive them.
        stale = self._clock() - retention_days * DAY_MS / 1000
        for backup in self._backups():
            if backup.stat().st_mtime < stale:
                shred(backup)
                log.info(
                    "removed %s, which is older than %d days",
                    backup.name,
                    retention_days,
                )
        return count

    # projects

    _PROJECT_SELECT = (
        "SELECT p.id, p.name, p.instructions, p.created_at, p.updated_at,"
        " (SELECT COUNT(*) FROM chats c WHERE c.project_id = p.id) AS chat_count FROM projects p"
    )

    def create_project(self, name: str, instructions: str = "") -> Project:
        now = self._now()
        project = Project(secrets.token_hex(8), name, instructions, now, now)
        with self._tx() as db:
            db.execute(
                "INSERT INTO projects (id, name, instructions, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (project.id, name, instructions, now, now),
            )
        return project

    def get_project(self, project_id: str) -> Project | None:
        with self._lock:
            row = self._db.execute(
                f"{self._PROJECT_SELECT} WHERE p.id = ?", (project_id,)
            ).fetchone()
        return Project(**dict(row)) if row else None

    def list_projects(self) -> list[Project]:
        """By name, without regard to case."""
        with self._lock:
            rows = self._db.execute(
                f"{self._PROJECT_SELECT} ORDER BY lower(p.name), p.id"
            ).fetchall()
        return [Project(**dict(r)) for r in rows]

    def count_projects(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM projects").fetchone()[0]

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        instructions: str | None = None,
    ) -> Project | None:
        """Change what is given. A project's own edits do not touch its chats' retention clocks."""
        changes = {"name": name, "instructions": instructions}
        columns = [c for c, value in changes.items() if value is not None]
        if columns:
            assignments = ", ".join(f"{c} = ?" for c in (*columns, "updated_at"))
            with self._tx() as db:
                db.execute(
                    f"UPDATE projects SET {assignments} WHERE id = ?",
                    [*(changes[c] for c in columns), self._now(), project_id],
                )
        return self.get_project(project_id)

    def delete_project(self, project_id: str, *, delete_chats: bool) -> bool:
        """The chats stay, without a project, unless `delete_chats`: then they go, with their text and pictures."""
        with self._tx() as db:
            removed = 0
            if delete_chats:
                removed = db.execute(
                    "DELETE FROM chats WHERE project_id = ?", (project_id,)
                ).rowcount
            deleted = (
                db.execute("DELETE FROM projects WHERE id = ?", (project_id,)).rowcount
                > 0
            )
        if removed:
            self._wipe_wal()
        return deleted

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
        images: Sequence[ImageRow] = (),
    ) -> Message:
        """Store a message and its images in one transaction: either all of it is kept or none."""
        now = self._now()
        stored = [
            {
                "id": secrets.token_hex(8),
                "w": image.width,
                "h": image.height,
                "seed": image.seed,
                "steps": image.steps,
                "model": image.model,
                "ref": image.ref,
            }
            for image in images
        ]
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
                stored or None,
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
            for image, meta in zip(images, stored, strict=True):
                db.execute(
                    "INSERT INTO images (id, message_id, chat_id, mime, width, height, seed, steps, model, ref,"
                    " bytes, data, created_at) VALUES (?, ?, ?, 'image/webp', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        meta["id"],
                        message.id,
                        chat_id,
                        image.width,
                        image.height,
                        image.seed,
                        image.steps,
                        image.model,
                        image.ref,
                        len(image.data),
                        image.data,
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
            # Metadata columns only: the bytes stay in the database until get_image asks for them.
            pictures = self._db.execute(
                "SELECT id, message_id, width, height, seed, steps, model, ref FROM images"
                " WHERE chat_id = ? ORDER BY rowid",
                (chat_id,),
            ).fetchall()
        by_message = defaultdict(list)
        for pic in pictures:
            by_message[pic["message_id"]].append(
                {
                    "id": pic["id"],
                    "w": pic["width"],
                    "h": pic["height"],
                    "seed": pic["seed"],
                    "steps": pic["steps"],
                    "model": pic["model"],
                    "ref": pic["ref"],
                }
            )
        return [_message(r, by_message.get(r["id"])) for r in rows]

    def get_image(self, chat_id: str, image_id: str) -> tuple[str, bytes] | None:
        """(mime type, bytes) of an image, only when it belongs to that chat."""
        with self._lock:
            row = self._db.execute(
                "SELECT mime, data FROM images WHERE id = ? AND chat_id = ?",
                (image_id, chat_id),
            ).fetchone()
        return (row["mime"], row["data"]) if row else None

    def image_bytes_total(self) -> int:
        with self._lock:
            return self._db.execute(
                "SELECT COALESCE(SUM(bytes), 0) FROM images"
            ).fetchone()[0]

    def delete_message(self, message_id: str) -> None:
        with self._tx() as db:
            had_images = db.execute(
                "SELECT EXISTS (SELECT 1 FROM images WHERE message_id = ?)",
                (message_id,),
            ).fetchone()[0]
            db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        if had_images:
            self._wipe_wal()

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
