import json
import logging
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from aihub_web import db as db_module
from aihub_web.db import ImageRow, Store, Task

FIXTURES = Path(__file__).parent / "fixtures" / "images"

DAY = 86_400


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, *, days: float = 0, seconds: float = 0) -> None:
        self.now += days * DAY + seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock):
    s = Store(tmp_path / "state" / "chats.db", clock)
    yield s
    s.close()


def make_chat(store: Store, title: str = "t", text: str = "hello") -> str:
    chat = store.create_chat(title, "ollama/qwen3:8b", "off")
    store.add_message(chat.id, "user", text)
    return chat.id


class TestRetention:
    def test_chat_at_exactly_the_limit_is_kept(
        self, store: Store, clock: Clock
    ) -> None:
        chat_id = make_chat(store)
        clock.advance(days=30)
        assert store.purge_expired(30) == 0
        assert store.get_chat(chat_id) is not None

    def test_chat_one_second_past_the_limit_is_deleted_with_its_messages(
        self, store: Store, clock: Clock
    ) -> None:
        chat_id = make_chat(store)
        clock.advance(days=30, seconds=1)
        assert store.purge_expired(30) == 1
        assert store.get_chat(chat_id) is None
        assert store.messages(chat_id) == []

    def test_a_new_message_restarts_the_clock(self, store: Store, clock: Clock) -> None:
        chat_id = make_chat(store)
        clock.advance(days=29)
        store.add_message(chat_id, "assistant", "still here")
        clock.advance(days=29)
        assert store.purge_expired(30) == 0

    def test_renaming_is_not_activity(self, store: Store, clock: Clock) -> None:
        chat_id = make_chat(store)
        clock.advance(days=29)
        store.update_chat(chat_id, {"title": "renamed"})
        clock.advance(days=2)
        assert store.purge_expired(30) == 1

    def test_only_expired_chats_go(self, store: Store, clock: Clock) -> None:
        old = make_chat(store, "old")
        clock.advance(days=40)
        fresh = make_chat(store, "fresh")
        assert store.purge_expired(30) == 1
        assert store.get_chat(old) is None
        assert store.get_chat(fresh) is not None

    def test_deleted_text_is_gone_from_the_file_and_the_wal(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = tmp_path / "chats.db"
        s = Store(path, clock)
        marker = "zebra-marker-7f3a91"
        make_chat(s, "secret", f"please forget {marker}")
        assert (
            marker.encode() in path.read_bytes()
            or marker.encode() in (tmp_path / "chats.db-wal").read_bytes()
        )
        clock.advance(days=31)
        s.purge_expired(30)
        raw = path.read_bytes()
        wal = tmp_path / "chats.db-wal"
        if wal.exists():
            raw += wal.read_bytes()
        s.close()
        assert marker.encode() not in raw

    def test_delete_all(self, store: Store) -> None:
        make_chat(store)
        make_chat(store)
        assert store.delete_all() == 2
        assert store.list_chats() == []


class TestChats:
    def test_list_is_newest_activity_first(self, store: Store, clock: Clock) -> None:
        a = make_chat(store, "a")
        clock.advance(seconds=5)
        b = make_chat(store, "b")
        clock.advance(seconds=5)
        store.add_message(a, "assistant", "reply")
        assert [c.id for c in store.list_chats()] == [a, b]

    def test_messages_keep_order_and_fields(self, store: Store) -> None:
        chat_id = make_chat(store)
        store.add_message(
            chat_id,
            "assistant",
            "answer",
            thinking="hmm",
            model="ollama/qwen3:8b",
            effort="low",
            usage={"out": 12, "cost": "0.001"},
            status="stopped",
            error={"code": "x", "message": "y"},
        )
        first, second = store.messages(chat_id)
        assert (first.seq, first.role) == (1, "user")
        assert (second.seq, second.status, second.thinking) == (2, "stopped", "hmm")
        assert second.usage == {"out": 12, "cost": "0.001"}
        assert second.error == {"code": "x", "message": "y"}

    def test_update_chat_only_touches_the_given_columns(self, store: Store) -> None:
        chat_id = make_chat(store)
        chat = store.update_chat(
            chat_id, {"model": "openrouter/x:free", "effort": None}
        )
        assert chat is not None
        assert (chat.title, chat.model, chat.effort) == ("t", "openrouter/x:free", None)

    def test_update_chat_ignores_unknown_columns(self, store: Store) -> None:
        chat_id = make_chat(store)
        before = store.get_chat(chat_id)
        assert store.update_chat(chat_id, {"id": "evil", "updated_at": "0"}) == before

    def test_delete_message_and_chat(self, store: Store) -> None:
        chat_id = make_chat(store)
        reply = store.add_message(chat_id, "assistant", "x")
        store.delete_message(reply.id)
        assert len(store.messages(chat_id)) == 1
        assert store.delete_chat(chat_id) is True
        assert store.delete_chat(chat_id) is False

    def test_messages_need_a_real_chat(self, store: Store) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            store.add_message("missing", "user", "x")

    def test_settings_round_trip(self, store: Store) -> None:
        assert store.get_setting("instructions") == ""
        store.set_setting("instructions", "be brief")
        store.set_setting("instructions", "be briefer")
        assert store.get_setting("instructions") == "be briefer"


class TestSearch:
    def test_matches_titles_and_message_text_with_a_snippet(self, store: Store) -> None:
        by_title = make_chat(store, "Pasta recipes", "unrelated")
        by_text = make_chat(
            store, "Chat", "x" * 120 + " the quick brown fox " + "y" * 120
        )
        make_chat(store, "Other", "nothing here")
        found = {c.id: c for c in store.list_chats("fox")}
        assert set(found) == {by_text}
        assert found[by_text].snippet is not None
        assert "quick brown fox" in found[by_text].snippet
        assert found[by_text].snippet.startswith("…")
        assert {c.id for c in store.list_chats("PASTA")} == {by_title}

    def test_a_snippet_shows_the_words_without_their_code_markup(
        self, store: Store
    ) -> None:
        chat = make_chat(
            store, "Chat", "Use `asyncio`:\n```python\nimport asyncio\n```"
        )
        [found] = store.list_chats("asyncio")
        assert found.id == chat
        assert found.snippet == "Use asyncio: import asyncio"

    @pytest.mark.parametrize(
        ("text", "term", "snippet"),
        [
            ("Run ```npm-install``` first", "npm-install", "Run npm-install first"),
            ("use ````md fences```` here", "fences", "use md fences here"),
            ("```js\nconst a = 1\n```", "const", "const a = 1"),
        ],
    )
    def test_triple_backticks_inside_a_line_keep_their_words(
        self, store: Store, text: str, term: str, snippet: str
    ) -> None:
        """Only a fence on a line of its own carries a language tag; inline code is just text."""
        make_chat(store, "Chat", text)
        [found] = store.list_chats(term)
        assert found.snippet == snippet

    @pytest.mark.parametrize(
        ("term", "decoy"),
        [("100%", "1000 reasons"), ("a_b", "axb"), ("back\\slash", "backslash")],
    )
    def test_wildcards_are_literal(self, store: Store, term: str, decoy: str) -> None:
        """Each decoy would match the term if its special character were still a wildcard."""
        make_chat(store, "decoy", decoy)
        literal = make_chat(store, "x", f"contains {term} verbatim")
        assert [c.id for c in store.list_chats(term)] == [literal]

    def test_blank_query_lists_everything(self, store: Store) -> None:
        make_chat(store)
        assert len(store.list_chats("   ")) == 1


class TestFile:
    def test_files_are_private(self, tmp_path: Path, clock: Clock) -> None:
        s = Store(tmp_path / "state" / "chats.db", clock)
        make_chat(s)
        mode = lambda p: stat.S_IMODE(p.stat().st_mode)
        assert mode(tmp_path / "state") == 0o700
        assert mode(tmp_path / "state" / "chats.db") == 0o600
        assert mode(tmp_path / "state" / "chats.db-wal") == 0o600
        s.close()

    def test_a_newer_schema_is_refused(self, tmp_path: Path, clock: Clock) -> None:
        path = tmp_path / "chats.db"
        Store(path, clock).close()
        raw = sqlite3.connect(path)
        raw.execute("PRAGMA user_version = 99")
        raw.close()
        with pytest.raises(RuntimeError, match="newer"):
            Store(path, clock)

    def test_reopening_keeps_the_data(self, tmp_path: Path, clock: Clock) -> None:
        path = tmp_path / "chats.db"
        s = Store(path, clock)
        chat_id = make_chat(s)
        s.close()
        again = Store(path, clock)
        assert again.get_chat(chat_id) is not None
        again.close()


def image(data: bytes = b"RIFF\0\0\0\0WEBPpixels", **fields) -> ImageRow:
    """The database keeps the bytes as they are, so any bytes will do."""
    return ImageRow(
        **{
            "width": 1024,
            "height": 576,
            "seed": 7,
            "steps": 8,
            "model": "images/zimage",
            **fields,
        },
        data=data,
    )


def make_image_chat(store: Store, data: bytes = b"RIFF\0\0\0\0WEBPpixels"):
    chat_id = make_chat(store, "fox", "a red fox")
    reply = store.add_message(
        chat_id, "assistant", "", model="images/zimage", images=[image(data)]
    )
    return chat_id, reply


class TestImages:
    def test_an_image_reply_keeps_its_metadata_and_its_bytes(
        self, store: Store
    ) -> None:
        chat_id = make_chat(store)
        reply = store.add_message(
            chat_id, "assistant", "", model="images/zimage", images=[image(b"webp-1")]
        )
        [stored] = reply.images
        assert re.fullmatch(r"[0-9a-f]{16}", stored["id"])
        assert stored == {
            "id": stored["id"],
            "w": 1024,
            "h": 576,
            "seed": 7,
            "steps": 8,
            "model": "images/zimage",
            "ref": None,
        }
        user, again = store.messages(chat_id)
        assert user.images is None
        assert again.images == reply.images
        assert store.get_image(chat_id, stored["id"]) == ("image/webp", b"webp-1")

    def test_messages_carry_metadata_never_the_bytes(self, store: Store) -> None:
        chat_id, _ = make_image_chat(store, b"x" * 50_000)
        reply = store.messages(chat_id)[-1].to_json()
        assert len(json.dumps(reply)) < 1000
        assert set(reply["images"][0]) == {
            "id",
            "w",
            "h",
            "seed",
            "steps",
            "model",
            "ref",
        }

    def test_a_text_reply_has_no_images(self, store: Store) -> None:
        chat_id = make_chat(store)
        reply = store.add_message(chat_id, "assistant", "words")
        assert reply.images is None and reply.to_json()["images"] is None
        assert store.messages(chat_id)[-1].images is None

    def test_an_image_can_only_be_read_through_its_own_chat(self, store: Store) -> None:
        mine, reply = make_image_chat(store)
        other = make_chat(store, "other")
        image_id = reply.images[0]["id"]
        assert store.get_image(mine, image_id) is not None
        assert store.get_image(other, image_id) is None
        assert store.get_image(mine, "0123456789abcdef") is None
        assert store.get_image("0123456789abcdef", image_id) is None

    def test_the_stored_bytes_add_up(self, store: Store) -> None:
        assert store.image_bytes_total() == 0
        make_image_chat(store, b"a" * 1000)
        chat_id, reply = make_image_chat(store, b"b" * 2500)
        assert store.image_bytes_total() == 3500
        store.delete_message(reply.id)
        assert store.image_bytes_total() == 1000
        store.delete_chat(chat_id)
        assert store.image_bytes_total() == 1000

    def test_a_reply_and_its_images_are_stored_together_or_not_at_all(
        self, store: Store
    ) -> None:
        chat_id = make_chat(store)
        with pytest.raises(sqlite3.IntegrityError):
            store.add_message(
                chat_id, "assistant", "", images=[image(), image(width=None)]
            )
        assert [m.role for m in store.messages(chat_id)] == ["user"]
        assert store.image_bytes_total() == 0

    def test_an_edit_remembers_the_image_it_started_from(self, store: Store) -> None:
        chat_id, first = make_image_chat(store)
        reference = first.images[0]["id"]
        edit = store.add_message(
            chat_id, "assistant", "", images=[image(ref=reference)]
        )
        assert edit.images[0]["ref"] == reference
        assert store.messages(chat_id)[-1].images[0]["ref"] == reference

    def test_images_come_back_in_the_order_they_were_stored(self, store: Store) -> None:
        chat_id = make_chat(store)
        store.add_message(
            chat_id, "assistant", "", images=[image(seed=1), image(seed=2)]
        )
        [reply] = store.messages(chat_id)[1:]
        assert [i["seed"] for i in reply.images] == [1, 2]

    def test_only_webp_can_be_stored(self, tmp_path: Path) -> None:
        path = tmp_path / "chats.db"
        s = Store(path)
        make_image_chat(s)
        s.close()
        raw = sqlite3.connect(path)
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            raw.execute("UPDATE images SET mime = 'image/png'")
        raw.close()


def seeded_deletions(clock: Clock):
    """Each way a chat or reply can be deleted, as a function of (store, chat id, reply)."""

    def expire(s: Store, chat_id: str, reply) -> None:
        clock.advance(days=31)
        s.purge_expired(30)

    return {
        "delete_chat": lambda s, chat_id, reply: s.delete_chat(chat_id),
        "delete_all": lambda s, chat_id, reply: s.delete_all(),
        "purge_expired": expire,
        "delete_message": lambda s, chat_id, reply: s.delete_message(reply.id),
    }


DELETIONS = ["delete_chat", "delete_all", "purge_expired", "delete_message"]


class TestImageRetention:
    @pytest.mark.parametrize("how", DELETIONS)
    def test_every_delete_path_takes_the_image_rows_with_it(
        self, store: Store, clock: Clock, how: str
    ) -> None:
        chat_id, reply = make_image_chat(store, b"gone" * 100)
        seeded_deletions(clock)[how](store, chat_id, reply)
        assert store.image_bytes_total() == 0
        assert store.get_image(chat_id, reply.images[0]["id"]) is None

    @pytest.mark.parametrize("how", ["delete_chat", "delete_message"])
    def test_deleting_one_chat_or_reply_leaves_other_images_alone(
        self, store: Store, clock: Clock, how: str
    ) -> None:
        keep_chat, keep = make_image_chat(store, b"keep" * 100)
        chat_id, reply = make_image_chat(store, b"gone" * 100)
        seeded_deletions(clock)[how](store, chat_id, reply)
        assert store.get_image(keep_chat, keep.images[0]["id"]) == (
            "image/webp",
            b"keep" * 100,
        )
        assert store.image_bytes_total() == 400

    def test_expiry_takes_only_the_images_of_expired_chats(
        self, store: Store, clock: Clock
    ) -> None:
        old_chat, old = make_image_chat(store, b"old!" * 100)
        clock.advance(days=20)
        fresh_chat, fresh = make_image_chat(store, b"new!" * 100)
        clock.advance(days=11)
        assert store.purge_expired(30) == 1
        assert store.get_image(old_chat, old.images[0]["id"]) is None
        assert store.get_image(fresh_chat, fresh.images[0]["id"]) is not None

    @pytest.mark.parametrize("how", DELETIONS)
    def test_deleted_image_bytes_are_gone_from_the_file_and_the_wal(
        self, tmp_path: Path, clock: Clock, how: str
    ) -> None:
        path = tmp_path / "chats.db"
        s = Store(path, clock)
        # Many pages of it, so most of the image sits in overflow pages. The search string is short
        # because each overflow page starts with a pointer that breaks up anything longer than a page.
        marker = b"pixel-marker-5c0de-"
        chat_id, reply = make_image_chat(s, b"RIFF\0\0\0\0WEBP" + marker * 1000)
        wal = tmp_path / "chats.db-wal"
        assert marker in path.read_bytes() + wal.read_bytes()
        seeded_deletions(clock)[how](s, chat_id, reply)
        raw = path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
        s.close()
        assert marker not in raw


def user_version(raw: sqlite3.Connection) -> int:
    return raw.execute("PRAGMA user_version").fetchone()[0]


# The columns the version 1 tables had: later versions add columns, and what they must keep is these.
V1_COLUMNS = {
    "chats": "id, title, model, effort, created_at, updated_at",
    "messages": "*",
    "settings": "*",
}


def rows_of(path: Path) -> dict[str, list[tuple]]:
    """Every row of the version 1 tables, read without going through the app."""
    raw = sqlite3.connect(path)
    try:
        return {
            table: raw.execute(f"SELECT {columns} FROM {table} ORDER BY 1").fetchall()
            for table, columns in V1_COLUMNS.items()
        }
    finally:
        raw.close()


def schema_of(path: Path) -> list[tuple]:
    raw = sqlite3.connect(path)
    try:
        return raw.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
    finally:
        raw.close()


def v1_copy(tmp_path: Path, name: str = "chats.db") -> Path:
    path = tmp_path / name
    shutil.copy(FIXTURES / "chats-v1.db", path)
    return path


def v2_copy(tmp_path: Path, name: str = "chats.db") -> Path:
    path = tmp_path / name
    shutil.copy(FIXTURES / "chats-v2.db", path)
    return path


def v3_copy(tmp_path: Path, name: str = "chats.db") -> Path:
    path = tmp_path / name
    shutil.copy(FIXTURES / "chats-v3.db", path)
    return path


class TestMigration:
    """chats-v1.db is a real version 1 file: the db.py of commit 77f7456 wrote it in a temporary directory.

    It holds 3 chats (done, stopped and failed replies, thinking, usage, a long message with accents) and
    8 messages, plus the standing instructions.
    """

    def test_the_fixture_really_is_a_version_1_file(self, tmp_path: Path) -> None:
        raw = sqlite3.connect(v1_copy(tmp_path))
        tables = {
            name
            for (name,) in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert user_version(raw) == 1
        assert tables == {"chats", "messages", "settings"}
        assert [
            raw.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("chats", "messages", "settings")
        ] == [3, 8, 1]
        raw.close()

    def test_opening_it_adds_the_images_table_and_keeps_every_row(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        before = rows_of(v1_copy(tmp_path, "untouched.db"))
        path = v1_copy(tmp_path)
        Store(path, clock).close()
        assert rows_of(path) == before
        raw = sqlite3.connect(path)
        assert user_version(raw) == db_module.SCHEMA_VERSION
        assert {
            name
            for (name,) in raw.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'images%'"
            )
        } == {"images", "images_message", "images_chat"}
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        raw.close()

    def test_an_upgraded_database_stores_and_serves_images(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        s = Store(v1_copy(tmp_path), clock)
        trip = next(c for c in s.list_chats() if c.title == "Plan the trip")
        reply = s.add_message(
            trip.id, "assistant", "", images=[image(b"after-the-upgrade")]
        )
        assert s.get_image(trip.id, reply.images[0]["id"]) == (
            "image/webp",
            b"after-the-upgrade",
        )
        assert len(s.messages(trip.id)) == 5
        s.delete_chat(trip.id)
        assert s.image_bytes_total() == 0
        assert len(s.list_chats()) == 2
        s.close()

    def test_an_upgraded_file_has_the_schema_of_a_new_one(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        fresh = tmp_path / "fresh.db"
        Store(fresh, clock).close()
        upgraded = v1_copy(tmp_path)
        Store(upgraded, clock).close()
        assert schema_of(upgraded) == schema_of(fresh)
        raw = sqlite3.connect(fresh)
        assert user_version(raw) == db_module.SCHEMA_VERSION
        raw.close()

    def test_opening_a_version_2_file_again_changes_nothing(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v1_copy(tmp_path)
        Store(path, clock).close()
        schema, rows = schema_of(path), rows_of(path)
        Store(path, clock).close()
        assert (schema_of(path), rows_of(path)) == (schema, rows)

    def test_rolling_back_by_hand_keeps_the_cascade(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        """PRAGMA user_version = 1 lets the old code open the file; it ignores the images table, and the
        foreign key still removes the rows when the old code deletes a chat."""
        path = tmp_path / "chats.db"
        s = Store(path, clock)
        make_image_chat(s)
        s.close()
        raw = sqlite3.connect(path)
        raw.execute("PRAGMA user_version = 1")
        raw.execute("PRAGMA foreign_keys = ON")
        raw.execute("DELETE FROM chats")
        assert raw.execute("SELECT count(*) FROM images").fetchone()[0] == 0
        raw.close()

    def test_the_steps_are_numbered_without_gaps_and_the_last_one_is_the_version(
        self,
    ) -> None:
        steps = sorted(db_module.MIGRATIONS)
        assert steps == list(range(1, len(steps) + 1))
        assert db_module.SCHEMA_VERSION == steps[-1]

    def test_a_step_that_fails_leaves_an_upgraded_file_as_it_was(
        self, tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = v1_copy(tmp_path)
        before = (schema_of(path), rows_of(path))
        # The second statement fails, after the first has already run.
        monkeypatch.setitem(
            db_module.MIGRATIONS, 2, "CREATE TABLE images (x); CREATE TABLE images (x);"
        )
        with pytest.raises(sqlite3.OperationalError, match="already exists"):
            Store(path, clock)
        raw = sqlite3.connect(path)
        assert user_version(raw) == 1
        raw.close()
        assert (schema_of(path), rows_of(path)) == before

    def test_a_new_file_keeps_the_steps_it_finished_and_completes_on_the_next_open(
        self, tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "chats.db"
        with monkeypatch.context() as broken:
            broken.setitem(db_module.MIGRATIONS, 2, "CREATE TABLE images (x); SELECT")
            with pytest.raises(sqlite3.OperationalError):
                Store(path, clock)
        raw = sqlite3.connect(path)
        assert user_version(raw) == 1
        assert {
            name
            for (name,) in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } == {"chats", "messages", "settings"}
        raw.close()
        Store(path, clock).close()
        raw = sqlite3.connect(path)
        assert user_version(raw) == db_module.SCHEMA_VERSION
        assert raw.execute("SELECT count(*) FROM images").fetchone()[0] == 0
        raw.close()


def backups_of(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.v*.bak"))


def parts_of(path: Path) -> list[Path]:
    """What a copy is called until it is whole (and its rollback journal, while SQLite writes it)."""
    return sorted(path.parent.glob(f"{path.name}.v*.bak.part*"))


APP = Path(__file__).parents[1] / "app"


class TestBackup:
    """A schema step rewrites the whole file and the chats live nowhere else, so the file is copied first."""

    def test_upgrading_an_old_file_first_copies_it_as_it_was(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v1_copy(tmp_path)
        before = rows_of(path)
        Store(path, clock).close()
        [backup] = backups_of(path)
        assert backup.name == "chats.db.v1.bak"
        assert rows_of(backup) == before
        raw = sqlite3.connect(backup)
        assert user_version(raw) == 1
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert not raw.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'images'"
        ).fetchone(), "the copy is from before the upgrade"
        raw.close()

    def test_the_copy_is_private_and_a_single_file(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v1_copy(tmp_path)
        Store(path, clock).close()
        [backup] = backups_of(path)
        assert stat.S_IMODE(backup.stat().st_mode) == 0o600
        sqlite3.connect(backup).close()
        assert not (backup.parent / (backup.name + "-wal")).exists()
        assert not (backup.parent / (backup.name + "-shm")).exists()

    def test_a_new_file_and_a_current_one_are_not_copied(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = tmp_path / "chats.db"
        Store(path, clock).close()
        Store(path, clock).close()
        assert backups_of(path) == []

    def test_a_second_copy_never_replaces_the_first(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v1_copy(tmp_path)
        Store(path, clock).close()
        first = backups_of(path)[0].read_bytes()
        for leftover in tmp_path.glob("chats.db-*"):
            leftover.unlink()
        shutil.copy(
            FIXTURES / "chats-v1.db", path
        )  # an old file again, as after a rollback
        Store(path, clock).close()
        assert [b.name for b in backups_of(path)] == [
            "chats.db.v1-2.bak",
            "chats.db.v1.bak",
        ]
        assert backups_of(path)[1].read_bytes() == first

    def test_without_its_copy_the_upgrade_does_not_happen(
        self, tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = v1_copy(tmp_path)
        before = (schema_of(path), rows_of(path))
        real = sqlite3.connect

        def refuse_the_copy(target, *args, **kwargs):
            if str(target).endswith(".part"):
                raise sqlite3.OperationalError("database or disk is full")
            return real(target, *args, **kwargs)

        monkeypatch.setattr(db_module.sqlite3, "connect", refuse_the_copy)
        with pytest.raises(sqlite3.OperationalError, match="disk is full"):
            Store(path, clock)
        monkeypatch.undo()
        assert backups_of(path) == [], (
            "no half-made copy is left to be mistaken for one"
        )
        assert parts_of(path) == []
        assert (schema_of(path), rows_of(path)) == before
        raw = sqlite3.connect(path)
        assert user_version(raw) == 1
        raw.close()

    def test_a_copy_that_does_not_come_out_marked_is_refused_and_not_kept(
        self, tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A copy without the mark would never be expired, so the upgrade stops rather than leave one."""

        class ForgetsTheMark(sqlite3.Connection):
            def execute(self, sql, *args):
                if "application_id" in sql:
                    return self.cursor()
                return super().execute(sql, *args)

        path = v1_copy(tmp_path)
        before = (schema_of(path), rows_of(path))
        real = sqlite3.connect

        def connect(target, *args, **kwargs):
            if str(target).endswith(".part"):
                kwargs["factory"] = ForgetsTheMark
            return real(target, *args, **kwargs)

        monkeypatch.setattr(db_module.sqlite3, "connect", connect)
        with pytest.raises(OSError, match="did not come out"):
            Store(path, clock)
        monkeypatch.undo()
        assert backups_of(path) == [] and parts_of(path) == []
        assert (schema_of(path), rows_of(path)) == before

    def test_a_copy_goes_when_it_is_older_than_the_retention_period(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v1_copy(tmp_path)
        s = Store(path, clock)
        clock.advance(days=30)
        s.purge_expired(30)
        assert len(backups_of(path)) == 1, "at exactly the limit it stays, like a chat"
        clock.advance(seconds=1)
        s.purge_expired(30)
        assert backups_of(path) == []
        s.close()

    def test_a_deleted_copy_is_overwritten_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        marker = b"private-marker-9c1d-" * 5000
        target = tmp_path / "chats.db.v1.bak"
        target.write_bytes(marker)
        seen: list[bytes] = []
        real_unlink = Path.unlink

        def look_then_unlink(self: Path, *args, **kwargs) -> None:
            seen.append(self.read_bytes())
            real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", look_then_unlink)
        db_module.shred(target)
        assert seen == [bytes(len(marker))]
        assert not target.exists()

    def test_delete_all_takes_the_copies_with_it(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v1_copy(tmp_path)
        s = Store(path, clock)
        assert len(backups_of(path)) == 1
        s.delete_all()
        assert backups_of(path) == []
        s.close()

    def test_a_file_that_only_looks_like_a_copy_is_never_touched(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v1_copy(tmp_path)
        s = Store(path, clock)
        lookalike = tmp_path / "chats.db.v1-mine.bak"
        shutil.copy(
            FIXTURES / "chats-v1.db", lookalike
        )  # somebody else's copy: no mark in its header
        note = tmp_path / "chats.db.v9.bak"
        note.write_text("not even a database")
        clock.advance(days=400)
        s.purge_expired(30)
        s.delete_all()
        assert lookalike.exists() and note.exists()
        assert [b.name for b in backups_of(path)] == [
            "chats.db.v1-mine.bak",
            "chats.db.v9.bak",
        ]
        s.close()

    def test_a_link_that_looks_like_a_copy_is_not_followed_or_removed(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v1_copy(tmp_path)
        s = Store(path, clock)
        [real] = backups_of(path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        precious = elsewhere / "precious.bak"
        shutil.copy(
            real, precious
        )  # marked as a copy in its header, but not one of ours to remove
        before = precious.read_bytes()
        link = tmp_path / "chats.db.v1-link.bak"
        link.symlink_to(precious)
        clock.advance(days=400)
        s.purge_expired(30)
        s.delete_all()
        assert link.is_symlink() and precious.read_bytes() == before
        assert not real.exists(), "the real copy did go"
        s.close()

    def test_shredding_refuses_a_link(self, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.write_bytes(b"keep me")
        link = tmp_path / "link"
        link.symlink_to(target)
        with pytest.raises(OSError):
            db_module.shred(link)
        assert target.read_bytes() == b"keep me" and link.is_symlink()

    def test_a_copy_has_its_name_only_once_it_is_whole(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        """A process that dies while copying leaves a part, never a file under the name a restore would use."""
        path = v1_copy(tmp_path)
        dies_with_the_copy_made = (
            "import os, sys\n"
            f"sys.path.insert(0, {str(APP)!r})\n"
            "from pathlib import Path\n"
            "from aihub_web import db\n"
            "db.os.link = lambda *args, **kwargs: os._exit(9)\n"
            "db.Store(Path(sys.argv[1]))\n"
        )
        died = subprocess.run(
            [sys.executable, "-I", "-c", dies_with_the_copy_made, str(path)],
            check=False,
        )
        assert died.returncode == 9
        assert backups_of(path) == []
        assert [p.name for p in parts_of(path)] == ["chats.db.v1.bak.part"]
        Store(
            path, clock
        ).close()  # the next start sweeps the part away and makes the copy properly
        assert [b.name for b in backups_of(path)] == ["chats.db.v1.bak"]
        assert parts_of(path) == []
        assert rows_of(backups_of(path)[0]) == rows_of(v1_copy(tmp_path, "pristine.db"))

    def test_a_part_that_a_crash_left_is_removed_at_the_next_start_and_never_counted(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = tmp_path / "chats.db"
        Store(path, clock).close()
        secret = b"chat text from a copy that never finished"
        part = tmp_path / "chats.db.v2.bak.part"
        journal = tmp_path / "chats.db.v2.bak.part-journal"
        part.write_bytes(b"SQLite format 3\0" + secret)
        journal.write_bytes(secret)
        mine = tmp_path / "chats.db.v2.bak.part.keep"  # not a name this app makes
        mine.write_bytes(b"somebody else's")
        Store(path, clock).close()
        assert not part.exists() and not journal.exists()
        assert mine.read_bytes() == b"somebody else's"

    def test_a_crash_between_linking_the_copy_and_removing_the_part_loses_nothing(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v1_copy(tmp_path)
        Store(path, clock).close()
        [copy] = backups_of(path)
        whole = copy.read_bytes()
        part = tmp_path / "chats.db.v1.bak.part"
        os.link(copy, part)  # both names are one file, as at that moment
        Store(path, clock).close()
        assert not part.exists()
        assert copy.read_bytes() == whole

    def test_a_file_with_another_name_is_unlinked_not_zeroed(
        self, tmp_path: Path
    ) -> None:
        kept = tmp_path / "kept-elsewhere.db"
        kept.write_bytes(b"a copy somebody kept on purpose")
        ours = tmp_path / "chats.db.v1.bak"
        os.link(kept, ours)
        db_module.shred(ours)
        assert not ours.exists()
        assert kept.read_bytes() == b"a copy somebody kept on purpose"

    def test_a_copy_that_cannot_be_removed_does_not_stop_the_app(
        self, tmp_path: Path, clock: Clock, caplog: pytest.LogCaptureFixture
    ) -> None:
        path = v1_copy(tmp_path)
        s = Store(path, clock)
        [copy] = backups_of(path)
        copy.chmod(0o400)  # somebody made it read-only
        clock.advance(days=400)
        with caplog.at_level(logging.WARNING):
            s.purge_expired(30)
            assert s.delete_all() == 0  # the 400 days had already expired the chats
        assert copy.exists()
        assert caplog.text.count("could not remove chats.db.v1.bak") == 2
        copy.chmod(0o600)
        s.close()


class TestMigrationToProjects:
    """chats-v2.db is a real version 2 file: the db.py of commit 21d5fab (schema 2) opened chats-v1.db, which upgraded
    it, and then stored one chat with a generated image. It holds 4 chats, 10 messages, 1 image and 1 setting."""

    def test_the_fixture_really_is_a_version_2_file(self, tmp_path: Path) -> None:
        raw = sqlite3.connect(v2_copy(tmp_path))
        tables = {
            name
            for (name,) in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert user_version(raw) == 2
        assert tables == {"chats", "images", "messages", "settings"}
        assert [
            raw.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("chats", "messages", "images", "settings")
        ] == [4, 10, 1, 1]
        raw.close()

    def test_opening_it_adds_projects_and_keeps_every_row(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        untouched = v2_copy(tmp_path, "untouched.db")
        before = rows_of(untouched)
        pictures = sqlite3.connect(untouched).execute("SELECT * FROM images").fetchall()
        path = v2_copy(tmp_path)
        s = Store(path, clock)
        assert len(s.list_chats()) == 4
        assert s.list_projects() == []
        s.close()
        assert rows_of(path) == before
        raw = sqlite3.connect(path)
        assert raw.execute("SELECT * FROM images").fetchall() == pictures
        assert user_version(raw) == db_module.SCHEMA_VERSION
        assert raw.execute("SELECT count(*) FROM projects").fetchone()[0] == 0
        assert (
            raw.execute(
                "SELECT count(*) FROM chats WHERE project_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert {
            name
            for (name,) in raw.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'chats_%'"
            )
        } >= {"chats_updated", "chats_project"}
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        raw.close()

    def test_an_upgraded_file_has_the_schema_of_a_new_one(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        fresh = tmp_path / "fresh.db"
        Store(fresh, clock).close()
        from_v2 = v2_copy(tmp_path)
        Store(from_v2, clock).close()
        assert schema_of(from_v2) == schema_of(fresh)

    def test_the_copy_made_first_is_the_version_2_file_untouched(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v2_copy(tmp_path)
        Store(path, clock).close()
        [backup] = backups_of(path)
        assert backup.name == "chats.db.v2.bak"
        assert backup.read_bytes()[:16] == b"SQLite format 3\0", (
            "a database a person can open with any SQLite tool"
        )
        raw = sqlite3.connect(backup)
        assert user_version(raw) == 2
        assert raw.execute("SELECT count(*) FROM chats").fetchone()[0] == 4
        assert raw.execute("PRAGMA table_info(chats)").fetchall()[-1][1] != "project_id"
        raw.close()

    def test_a_version_1_file_goes_through_both_steps(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v1_copy(tmp_path)
        s = Store(path, clock)
        project = s.create_project("Trips")
        trip = next(c for c in s.list_chats() if c.title == "Plan the trip")
        s.update_chat(trip.id, {"project_id": project.id})
        assert s.get_chat(trip.id).project_id == project.id
        s.close()

    def test_old_code_cannot_open_a_version_3_file_and_says_why(
        self, tmp_path: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = v3_copy(tmp_path)  # a real version 3 file
        monkeypatch.setattr(db_module, "SCHEMA_VERSION", 2)
        with pytest.raises(
            RuntimeError, match=r"schema 3 is newer than this app \(2\)"
        ):
            Store(path, clock)


class TestProjects:
    def test_a_new_project(self, store: Store, clock: Clock) -> None:
        project = store.create_project("Trips", "Answer in French.")
        assert re.fullmatch(r"[0-9a-f]{16}", project.id)
        assert project.to_json() == {
            "id": project.id,
            "name": "Trips",
            "instructions": "Answer in French.",
            "created_at": int(clock.now * 1000),
            "updated_at": int(clock.now * 1000),
            "chat_count": 0,
        }
        assert store.get_project(project.id) == project
        assert store.get_project("0" * 16) is None

    def test_the_list_is_by_name_without_regard_to_case(self, store: Store) -> None:
        for name in ("beta", "Alpha", "alpha two", "Zed"):
            store.create_project(name)
        assert [p.name for p in store.list_projects()] == [
            "Alpha",
            "alpha two",
            "beta",
            "Zed",
        ]
        assert store.count_projects() == 4

    def test_editing_changes_only_what_is_given(
        self, store: Store, clock: Clock
    ) -> None:
        project = store.create_project("Trips", "Be brief.")
        clock.advance(seconds=5)
        renamed = store.update_project(project.id, name="Travel")
        assert (renamed.name, renamed.instructions) == ("Travel", "Be brief.")
        assert renamed.updated_at == project.updated_at + 5000
        assert renamed.created_at == project.created_at
        rewritten = store.update_project(project.id, instructions="")
        assert (rewritten.name, rewritten.instructions) == ("Travel", "")
        assert store.update_project("0" * 16, name="x") is None

    def test_a_chat_belongs_to_a_project(self, store: Store) -> None:
        project = store.create_project("Trips")
        inside = store.create_chat("in", "ollama/qwen3:8b", None, project.id)
        outside = store.create_chat("out", "ollama/qwen3:8b", None)
        assert inside.project_id == project.id and outside.project_id is None
        assert store.get_chat(inside.id).project_id == project.id
        assert inside.to_json()["project_id"] == project.id
        assert {c.id: c.project_id for c in store.list_chats()} == {
            inside.id: project.id,
            outside.id: None,
        }
        assert store.get_project(project.id).chat_count == 1
        store.add_message(inside.id, "user", "findme please")
        assert [c.project_id for c in store.list_chats("findme")] == [project.id]

    def test_a_chat_that_names_no_project_is_refused_by_the_database(
        self, store: Store
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            store.create_chat("x", "ollama/qwen3:8b", None, "f" * 16)

    def test_moving_a_chat_is_not_activity(self, store: Store, clock: Clock) -> None:
        project = store.create_project("Trips")
        chat = store.create_chat("c", "ollama/qwen3:8b", None)
        clock.advance(days=3)
        moved = store.update_chat(chat.id, {"project_id": project.id})
        assert moved.project_id == project.id
        assert moved.updated_at == chat.updated_at
        free = store.update_chat(chat.id, {"project_id": None})
        assert free.project_id is None and free.updated_at == chat.updated_at
        with pytest.raises(sqlite3.IntegrityError):
            store.update_chat(chat.id, {"project_id": "f" * 16})

    def test_deleting_a_project_can_keep_its_chats(self, store: Store) -> None:
        project = store.create_project("Trips")
        chat = store.create_chat("c", "ollama/qwen3:8b", None, project.id)
        store.add_message(chat.id, "user", "hello")
        assert store.delete_project(project.id, delete_chats=False) is True
        assert store.get_project(project.id) is None
        kept = store.get_chat(chat.id)
        assert kept is not None and kept.project_id is None
        assert len(store.messages(chat.id)) == 1
        assert store.delete_project(project.id, delete_chats=False) is False

    def test_deleting_a_project_can_take_its_chats_and_only_its_chats(
        self, store: Store
    ) -> None:
        project = store.create_project("Trips")
        other = store.create_project("Work")
        mine = [
            store.create_chat(f"c{i}", "ollama/qwen3:8b", None, project.id)
            for i in range(2)
        ]
        theirs = store.create_chat("w", "ollama/qwen3:8b", None, other.id)
        free = store.create_chat("f", "ollama/qwen3:8b", None)
        for chat in (*mine, theirs, free):
            store.add_message(chat.id, "user", "x")
        assert store.delete_project(project.id, delete_chats=True) is True
        assert [store.get_chat(c.id) for c in mine] == [None, None]
        assert store.messages(mine[0].id) == []
        assert store.get_chat(theirs.id).project_id == other.id
        assert store.get_chat(free.id) is not None
        assert store.delete_project("0" * 16, delete_chats=True) is False
        assert len(store.list_chats()) == 2

    def test_deleted_chat_text_leaves_the_file_with_the_project(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = tmp_path / "chats.db"
        s = Store(path, clock)
        project = s.create_project("Trips")
        chat = s.create_chat("c", "ollama/qwen3:8b", None, project.id)
        # The text is long, so it spans overflow pages whose pointers break it up: look for the short repeated unit.
        marker = b"project-marker-7f3a-"
        s.add_message(chat.id, "user", (marker * 300).decode())
        wal = tmp_path / "chats.db-wal"
        assert marker in path.read_bytes() + wal.read_bytes()
        s.delete_project(project.id, delete_chats=True)
        raw = path.read_bytes() + (wal.read_bytes() if wal.exists() else b"")
        s.close()
        assert marker not in raw

    def test_projects_outlive_their_chats(self, store: Store, clock: Clock) -> None:
        project = store.create_project("Trips", "Be brief.")
        chat = store.create_chat("c", "ollama/qwen3:8b", None, project.id)
        store.add_message(chat.id, "user", "x")
        clock.advance(days=400)
        assert store.purge_expired(30) == 1
        assert store.get_project(project.id).chat_count == 0
        other = store.create_chat("d", "ollama/qwen3:8b", None, project.id)
        store.add_message(other.id, "user", "y")
        assert store.delete_all() == 1
        assert [p.name for p in store.list_projects()] == ["Trips"]


DAILY = {"kind": "daily", "time": "08:00"}


def a_task(store: Store, name: str = "Morning digest", **fields) -> Task:
    fields.setdefault("prompt", "Summarise what I should do today.")
    fields.setdefault("model", "ollama/qwen3:8b")
    fields.setdefault("effort", None)
    fields.setdefault("schedule", DAILY)
    return store.create_task(name, **fields)


class TestMigrationToTasks:
    """chats-v3.db is a real version 3 file: the db.py of this branch before schema 4 opened chats-v2.db (which
    upgraded it) and then stored two projects and a chat in one of them. It holds 5 chats, 12 messages, 1 image,
    2 projects and 1 setting."""

    def test_the_fixture_really_is_a_version_3_file(self, tmp_path: Path) -> None:
        raw = sqlite3.connect(v3_copy(tmp_path))
        tables = {
            name
            for (name,) in raw.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert user_version(raw) == 3
        assert tables == {"chats", "images", "messages", "projects", "settings"}
        assert [
            raw.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("chats", "messages", "images", "projects", "settings")
        ] == [5, 12, 1, 2, 1]
        raw.close()

    def test_opening_it_adds_tasks_and_keeps_every_row(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        untouched = v3_copy(tmp_path, "untouched.db")
        before = rows_of(untouched)
        raw = sqlite3.connect(untouched)
        kept = {
            table: raw.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in ("images", "projects")
        }
        raw.close()
        path = v3_copy(tmp_path)
        s = Store(path, clock)
        assert s.list_tasks() == []
        assert [p.name for p in s.list_projects()] == ["Trips", "Work notes"]
        assert s.list_projects()[0].chat_count == 1
        s.close()
        assert rows_of(path) == before
        raw = sqlite3.connect(path)
        assert user_version(raw) == 4
        for table, rows in kept.items():
            assert raw.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows
        assert raw.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
        assert (
            raw.execute(
                "SELECT count(*) FROM chats WHERE task_id IS NOT NULL"
            ).fetchone()[0]
            == 0
        )
        assert raw.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert raw.execute("PRAGMA foreign_key_check").fetchall() == []
        raw.close()

    def test_an_upgraded_file_has_the_schema_of_a_new_one(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        fresh = tmp_path / "fresh.db"
        Store(fresh, clock).close()
        for old in (v1_copy, v2_copy, v3_copy):
            upgraded = old(tmp_path, f"from-{old.__name__}.db")
            Store(upgraded, clock).close()
            assert schema_of(upgraded) == schema_of(fresh)

    def test_the_copy_made_first_is_the_version_3_file(
        self, tmp_path: Path, clock: Clock
    ) -> None:
        path = v3_copy(tmp_path)
        Store(path, clock).close()
        [backup] = backups_of(path)
        assert backup.name == "chats.db.v3.bak"
        raw = sqlite3.connect(backup)
        assert user_version(raw) == 3
        assert raw.execute("SELECT count(*) FROM projects").fetchone()[0] == 2
        assert not any(
            row[1] == "task_id" for row in raw.execute("PRAGMA table_info(chats)")
        )
        raw.close()


class TestTasks:
    def test_a_new_task(self, store: Store, clock: Clock) -> None:
        made = a_task(store, effort="low", next_run_at=1_800_000_000_000)
        assert re.fullmatch(r"[0-9a-f]{16}", made.id)
        assert made.to_json() == {
            "id": made.id,
            "name": "Morning digest",
            "prompt": "Summarise what I should do today.",
            "model": "ollama/qwen3:8b",
            "effort": "low",
            "schedule": DAILY,
            "enabled": True,
            "project_id": None,
            "next_run_at": 1_800_000_000_000,
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "last_chat_id": None,
            "failures": 0,
            "created_at": int(clock.now * 1000),
            "updated_at": int(clock.now * 1000),
        }
        assert store.get_task(made.id) == made
        assert store.get_task("0" * 16) is None

    def test_the_list_is_by_name_without_regard_to_case(self, store: Store) -> None:
        for name in ("beta", "Alpha", "Zed", "alpha two"):
            a_task(store, name)
        assert [t.name for t in store.list_tasks()] == [
            "Alpha",
            "alpha two",
            "beta",
            "Zed",
        ]
        assert store.count_tasks() == 4

    def test_a_schedule_comes_back_as_it_went_in(self, store: Store) -> None:
        weekly = {"kind": "weekly", "day": 4, "time": "17:30"}
        assert a_task(store, schedule=weekly).schedule == weekly
        assert store.list_tasks()[0].schedule == weekly

    def test_editing_changes_only_what_is_given_and_ignores_other_names(
        self, store: Store, clock: Clock
    ) -> None:
        made = a_task(store)
        clock.advance(seconds=5)
        changed = store.update_task(
            made.id,
            {
                "name": "Evening digest",
                "schedule": {"kind": "hourly", "every": 6},
                "enabled": False,
                "id": "0" * 16,  # not a column a caller may set
                "created_at": 1,
            },
        )
        assert changed.name == "Evening digest" and changed.enabled is False
        assert changed.schedule == {"kind": "hourly", "every": 6}
        assert changed.prompt == made.prompt and changed.id == made.id
        assert changed.created_at == made.created_at
        assert changed.updated_at == made.updated_at + 5000
        assert store.update_task("0" * 16, {"name": "x"}) is None

    def test_what_a_run_leaves_behind_is_kept(self, store: Store) -> None:
        made = a_task(store)
        chat = store.create_chat(
            "Morning digest, 20 Oct", made.model, None, None, made.id
        )
        done = store.update_task(
            made.id,
            {
                "last_run_at": 5,
                "last_status": "error",
                "last_error": "The model stopped responding.",
                "last_chat_id": chat.id,
                "failures": 2,
                "next_run_at": 99,
            },
        )
        assert (done.last_status, done.last_error, done.failures) == (
            "error",
            "The model stopped responding.",
            2,
        )
        assert (done.last_chat_id, done.next_run_at, done.last_run_at) == (
            chat.id,
            99,
            5,
        )
        assert store.get_chat(chat.id).to_json()["task_id"] == made.id
        with pytest.raises(sqlite3.IntegrityError):
            store.update_task(made.id, {"last_status": "exploded"})

    def test_due_tasks_are_the_enabled_ones_whose_time_has_come_in_order(
        self, store: Store
    ) -> None:
        late = a_task(store, "late", next_run_at=300)
        early = a_task(store, "early", next_run_at=100)
        exactly = a_task(store, "exactly", next_run_at=200)
        a_task(store, "later", next_run_at=201)
        a_task(store, "off", next_run_at=50, enabled=False)
        a_task(store, "unscheduled", next_run_at=None)
        assert [t.id for t in store.due_tasks(200)] == [early.id, exactly.id]
        assert [t.id for t in store.due_tasks(10_000)][-1] == late.id
        assert store.due_tasks(99) == []

    def test_a_chat_that_names_no_task_is_refused_by_the_database(
        self, store: Store
    ) -> None:
        with pytest.raises(sqlite3.IntegrityError):
            store.create_chat("x", "ollama/qwen3:8b", None, None, "f" * 16)

    def test_deleting_a_task_keeps_its_chats(self, store: Store) -> None:
        made = a_task(store)
        chat = store.create_chat("a run", made.model, None, None, made.id)
        store.add_message(chat.id, "user", "x")
        assert store.delete_task(made.id) is True
        assert store.get_task(made.id) is None
        assert store.get_chat(chat.id).to_json()["task_id"] is None
        assert len(store.messages(chat.id)) == 1
        assert store.delete_task(made.id) is False

    def test_deleting_a_chat_or_a_project_leaves_the_task(self, store: Store) -> None:
        project = store.create_project("Digests")
        made = a_task(store, project_id=project.id)
        chat = store.create_chat("a run", made.model, None, project.id, made.id)
        store.update_task(made.id, {"last_chat_id": chat.id})
        store.delete_chat(chat.id)
        assert store.get_task(made.id).last_chat_id is None
        store.delete_project(project.id, delete_chats=False)
        assert store.get_task(made.id).project_id is None

    def test_tasks_outlive_retention_and_delete_all(
        self, store: Store, clock: Clock
    ) -> None:
        made = a_task(store, "Keeps", next_run_at=5)
        chat = store.create_chat("a run", made.model, None, None, made.id)
        store.add_message(chat.id, "user", "x")
        clock.advance(days=400)
        assert store.purge_expired(30) == 1
        other = store.create_chat("another", made.model, None, None, made.id)
        store.add_message(other.id, "user", "y")
        assert store.delete_all() == 1
        assert [t.name for t in store.list_tasks()] == ["Keeps"]
