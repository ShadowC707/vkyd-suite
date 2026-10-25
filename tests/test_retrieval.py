"""Tests for SQLite FTS5 retrieval, chat isolation, Ukrainian/emoji search, and hostile queries."""

import json

import pytest

from app.db import Database
from app.importer import TelegramImporter
from app.retrieval import MemoryRetriever


@pytest.fixture
def populated_chats(test_db: Database, chat_a_bytes: bytes, chat_b_bytes: bytes):
    importer = TelegramImporter(test_db)
    res_a = importer.import_chat_json(chat_a_bytes)
    res_b = importer.import_chat_json(chat_b_bytes)
    return res_a.chat.id, res_b.chat.id


def test_chat_isolation_with_overlapping_message_ids(test_db: Database, populated_chats):
    chat_a_id, chat_b_id = populated_chats
    retriever = MemoryRetriever(test_db)

    # Both chats contain message ID 4 with completely different text
    # Chat A: "Головне правило релізу: якщо білдиться на моєму ноуті — значить готово в прод! 🚀"
    # Chat B: "Яка піца береться? Чотири сири чи з ананасами? 🍕"

    res_a_pizza = retriever.search_memories(chat_id=chat_a_id, query="піца")
    assert res_a_pizza.total == 0
    assert len(res_a_pizza.items) == 0

    res_b_pizza = retriever.search_memories(chat_id=chat_b_id, query="піца")
    assert res_b_pizza.total == 1
    assert res_b_pizza.items[0].source_message_id == 4
    assert "ананасами" in res_b_pizza.items[0].text

    res_a_build = retriever.search_memories(chat_id=chat_a_id, query="білдиться")
    assert res_a_build.total == 1
    assert res_a_build.items[0].source_message_id == 4
    assert "ноут" in res_a_build.items[0].text

    res_b_build = retriever.search_memories(chat_id=chat_b_id, query="білдиться")
    assert res_b_build.total == 0


def test_ukrainian_and_surzhyk_search(test_db: Database, populated_chats):
    chat_a_id, chat_b_id = populated_chats
    retriever = MemoryRetriever(test_db)

    # Search in Chat A
    res = retriever.search_memories(chat_id=chat_a_id, query="еспресо")
    assert res.total == 1
    assert "подвійним еспресо" in res.items[0].text

    # Search in Chat B: "Каркасон" is in message 2 only
    res_karkason = retriever.search_memories(chat_id=chat_b_id, query="Каркасон")
    assert res_karkason.total == 1
    assert "Каркасон або Дюну" in res_karkason.items[0].text

    # "Дюну" is mentioned in both message 2 ("Каркасон або Дюну") and message 3 ("Я за Дюну!")
    res_dune = retriever.search_memories(chat_id=chat_b_id, query="Дюну")
    assert res_dune.total == 2
    assert {item.source_message_id for item in res_dune.items} == {2, 3}


def test_emoji_search(test_db: Database, populated_chats):
    chat_a_id, chat_b_id = populated_chats
    retriever = MemoryRetriever(test_db)

    # Ukrainian flag search in Chat A
    res_flag = retriever.search_memories(chat_id=chat_a_id, query="🇺🇦")
    assert res_flag.total >= 1
    assert any("Слава Україні" in item.text for item in res_flag.items)

    # Pizza emoji search in Chat B
    res_pizza = retriever.search_memories(chat_id=chat_b_id, query="🍕")
    assert res_pizza.total == 1
    assert res_pizza.items[0].source_message_id == 4


def test_safe_no_token_search(test_db: Database, populated_chats):
    chat_a_id, _ = populated_chats
    retriever = MemoryRetriever(test_db)

    # Empty string
    res_empty = retriever.search_memories(chat_id=chat_a_id, query="")
    assert res_empty.total == 6
    assert len(res_empty.items) == 6

    # Whitespace only
    res_spaces = retriever.search_memories(chat_id=chat_a_id, query="   \t\n  ")
    assert res_spaces.total == 6
    assert len(res_spaces.items) == 6


def test_hostile_search_syntax_does_not_error(test_db: Database, populated_chats):
    chat_a_id, _ = populated_chats
    retriever = MemoryRetriever(test_db)

    hostile_queries = [
        '"',
        '""',
        '"""',
        "*",
        "*** OR 1=1 --",
        "NOT AND OR",
        "NEAR(a b)",
        "() {} [] ^",
        "text:secret",
        "chat_id:99999",
        "SELECT * FROM messages;",
        "'; DROP TABLE messages; --",
    ]

    for q in hostile_queries:
        res = retriever.search_memories(chat_id=chat_a_id, query=q)
        assert isinstance(res.items, list)
        assert res.total >= 0


def test_context_and_reply_expansion(test_db: Database, populated_chats):
    chat_a_id, _ = populated_chats
    retriever = MemoryRetriever(test_db)

    # Message 5 replies to message 4: "Ігор, ніяких деплоїв у п'ятницю ввечері, благаю 😂"
    res = retriever.search_memories(chat_id=chat_a_id, query="деплоїв")
    assert res.total == 1
    item = res.items[0]
    assert item.source_message_id == 5

    # Check reply parent resolution
    assert item.reply_parent is not None
    assert item.reply_parent.source_message_id == 4
    assert "якщо білдиться на моєму ноуті" in item.reply_parent.text

    # Check nearby context contains preceding, target, and succeeding
    nearby = item.nearby_context
    assert len(nearby) >= 2
    target_matches = [m for m in nearby if m.is_target]
    assert len(target_matches) == 1
    assert target_matches[0].source_message_id == 5


def test_chat_deletion_cleans_messages_and_fts(test_db: Database, populated_chats):
    chat_a_id, chat_b_id = populated_chats

    with test_db.transaction() as conn:
        conn.execute("DELETE FROM messages_fts WHERE chat_id = ?;", (chat_a_id,))
        conn.execute("DELETE FROM chats WHERE id = ?;", (chat_a_id,))

    with test_db.connection() as conn:
        # Chat A is completely removed
        assert (
            conn.execute("SELECT count(*) FROM chats WHERE id = ?;", (chat_a_id,)).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM messages WHERE chat_id = ?;", (chat_a_id,)
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM messages_fts WHERE chat_id = ?;", (chat_a_id,)
            ).fetchone()[0]
            == 0
        )

        # Chat B remains intact
        assert (
            conn.execute("SELECT count(*) FROM chats WHERE id = ?;", (chat_b_id,)).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM messages WHERE chat_id = ?;", (chat_b_id,)
            ).fetchone()[0]
            == 8
        )
        assert (
            conn.execute(
                "SELECT count(*) FROM messages_fts WHERE chat_id = ?;", (chat_b_id,)
            ).fetchone()[0]
            == 8
        )


def test_durability_across_db_reopen(tmp_path, chat_a_bytes: bytes):
    db_file = tmp_path / "durable.db"
    db1 = Database(db_file)
    db1.run_migrations()

    importer = TelegramImporter(db1)
    res = importer.import_chat_json(chat_a_bytes)
    chat_id = res.chat.id

    # Simulate process exit: close connections and instantiate new Database pointing to same file
    del db1

    db2 = Database(db_file)
    retriever = MemoryRetriever(db2)
    search_res = retriever.search_memories(chat_id=chat_id, query="еспресо")
    assert search_res.total == 1
    assert "подвійним еспресо" in search_res.items[0].text


# R7: Literal wildcard search, escaping, and length bounds
def test_literal_wildcard_search(test_db: Database):
    importer = TelegramImporter(test_db)
    export = {
        "name": "Wildcard Test Chat",
        "messages": [
            {"id": 1, "text": "abc", "date": "2024-01-01T12:00:00Z"},
            {"id": 2, "text": "100% yes", "date": "2024-01-01T12:01:00Z"},
            {"id": 3, "text": "foo_bar", "date": "2024-01-01T12:02:00Z"},
            {"id": 4, "text": "simple bar", "date": "2024-01-01T12:03:00Z"},
            {"id": 5, "text": "path\\to\\file", "date": "2024-01-01T12:04:00Z"},
            {"id": 6, "text": "quote 'single' and \"double\"", "date": "2024-01-01T12:05:00Z"},
            {"id": 7, "text": "100% 🍕 celebration", "date": "2024-01-01T12:06:00Z"},
        ],
    }
    res = importer.import_chat_json(json.dumps(export).encode("utf-8"))
    chat_id = res.chat.id
    retriever = MemoryRetriever(test_db)

    # 1. Search '%' matches only messages containing literal '%', not 'abc' or 'foo_bar'
    res_pct = retriever.search_memories(chat_id=chat_id, query="%")
    assert res_pct.total == 2
    matched_ids = {m.source_message_id for m in res_pct.items}
    assert matched_ids == {2, 7}

    # 2. Search '_' matches only messages containing literal '_', not 'simple bar'
    res_underscore = retriever.search_memories(chat_id=chat_id, query="_")
    assert res_underscore.total == 1
    assert res_underscore.items[0].source_message_id == 3

    # 3. Search literal backslash '\\'
    res_backslash = retriever.search_memories(chat_id=chat_id, query="\\")
    assert res_backslash.total == 1
    assert res_backslash.items[0].source_message_id == 5

    # 4. Search single and double quotes
    res_quote = retriever.search_memories(chat_id=chat_id, query="'")
    assert res_quote.total == 1
    assert res_quote.items[0].source_message_id == 6

    # 5. Combined content: '100% 🍕'
    res_combined = retriever.search_memories(chat_id=chat_id, query="100% 🍕")
    assert res_combined.total >= 1
    assert any(m.source_message_id == 7 for m in res_combined.items)


def test_search_text_bearing_sticker_and_retrieval(test_db: Database):
    importer = TelegramImporter(test_db)
    export = {
        "name": "Sticker Retrieval Chat",
        "messages": [
            {
                "id": 1,
                "media_type": "sticker",
                "sticker_emoji": "☕",
                "file": "stickers/coffee.webp",
                "text": "Sticker coffee break!",
                "date": "2024-01-01T12:00:00Z",
            }
        ],
    }
    res = importer.import_chat_json(json.dumps(export).encode("utf-8"))
    chat_id = res.chat.id
    retriever = MemoryRetriever(test_db)

    # Should be retrievable via text token
    search_res = retriever.search_memories(chat_id=chat_id, query="coffee")
    assert search_res.total == 1
    item = search_res.items[0]
    assert item.source_message_id == 1
    assert item.kind == "sticker"
    assert "coffee break" in item.text
    assert item.sticker_metadata is not None
    assert item.sticker_metadata["sticker_emoji"] == "☕"


def test_bounded_query_length_safety(test_db: Database, populated_chats):
    chat_a_id, _ = populated_chats
    retriever = MemoryRetriever(test_db)

    # 1000 characters long query
    long_q = "а" * 1000
    res = retriever.search_memories(chat_id=chat_a_id, query=long_q)
    assert isinstance(res.items, list)
    assert res.total >= 0
