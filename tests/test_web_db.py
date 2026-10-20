import sqlite3
import stat
from pathlib import Path

import pytest
from aihub_web.db import Store

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
