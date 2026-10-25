"""Tests for Telegram single-chat JSON importer conforming to R3, R4, R5."""

import json

import pytest

from app.db import Database
from app.importer import ImporterError, TelegramImporter
from app.predicates import is_supported_message, is_supported_text_content


def test_import_roundtrip(test_db: Database, chat_a_bytes: bytes):
    importer = TelegramImporter(test_db)
    result = importer.import_chat_json(chat_a_bytes)

    assert result.chat.name == "Devpost Hackathon Team"
    assert result.chat.source_chat_id == "100200300"
    assert result.report.total_records == 8
    assert result.report.supported_text_messages == 6
    assert result.report.service_records == 1
    assert result.report.stickers_count == 1
    assert result.report.unsupported_media_count == 1

    with test_db.connection() as conn:
        count = conn.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ?;", (result.chat.id,)
        ).fetchone()[0]
        assert count == 8


def test_rich_text_fidelity_and_reply_references(test_db: Database, chat_a_bytes: bytes):
    importer = TelegramImporter(test_db)
    result = importer.import_chat_json(chat_a_bytes)

    with test_db.connection() as conn:
        # Message 3: rich-text entity array
        row3 = conn.execute(
            """
            SELECT text, reply_to_source_id FROM messages
            WHERE chat_id = ? AND source_message_id = 3;
            """,
            (result.chat.id,),
        ).fetchone()
        assert row3 is not None
        expected_text = (
            "Я вже з подвійним еспресо! План простий: спочатку спека, потім код, без паніки."
        )
        assert row3["text"] == expected_text
        assert row3["reply_to_source_id"] == 2

        # Message 7: code entity + Ukrainian + emoji
        row7 = conn.execute(
            "SELECT text FROM messages WHERE chat_id = ? AND source_message_id = 7;",
            (result.chat.id,),
        ).fetchone()
        assert "uv run pytest -v" in row7["text"]
        assert "Слава Україні! 🇺🇦" in row7["text"]

        # Message 6: sticker metadata
        row6 = conn.execute(
            """
            SELECT kind, sticker_metadata_json FROM messages
            WHERE chat_id = ? AND source_message_id = 6;
            """,
            (result.chat.id,),
        ).fetchone()
        assert row6["kind"] == "sticker"
        assert "it_cat_coffee.webp" in row6["sticker_metadata_json"]
        assert "☕" in row6["sticker_metadata_json"]


def test_idempotent_reimport(test_db: Database, chat_a_bytes: bytes):
    importer = TelegramImporter(test_db)
    res1 = importer.import_chat_json(chat_a_bytes)
    res2 = importer.import_chat_json(chat_a_bytes)

    assert res1.chat.id == res2.chat.id
    assert res1.chat.import_fingerprint == res2.chat.import_fingerprint
    assert res1.report.total_records == res2.report.total_records
    assert res1.chat.supported_text_messages == res2.chat.supported_text_messages

    with test_db.connection() as conn:
        chat_count = conn.execute("SELECT count(*) FROM chats;").fetchone()[0]
        msg_count = conn.execute("SELECT count(*) FROM messages;").fetchone()[0]
        assert chat_count == 1
        assert msg_count == 8


def test_reject_account_export(test_db: Database, account_export_bytes: bytes):
    importer = TelegramImporter(test_db)
    with pytest.raises(ImporterError) as exc_info:
        importer.import_chat_json(account_export_bytes)
    assert exc_info.value.code == "account_export_rejected"

    with test_db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM chats;").fetchone()[0] == 0


def test_reject_duplicate_source_message_ids_with_atomic_rollback(
    test_db: Database, duplicate_ids_bytes: bytes
):
    importer = TelegramImporter(test_db)
    with pytest.raises(ImporterError) as exc_info:
        importer.import_chat_json(duplicate_ids_bytes)
    assert exc_info.value.code == "duplicate_source_message_ids"
    assert "Duplicate source message ID 2" in exc_info.value.message

    with test_db.connection() as conn:
        assert conn.execute("SELECT count(*) FROM chats;").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM messages;").fetchone()[0] == 0


def test_reject_malformed_json(test_db: Database, malformed_bytes: bytes):
    importer = TelegramImporter(test_db)
    with pytest.raises(ImporterError) as exc_info:
        importer.import_chat_json(malformed_bytes)
    assert exc_info.value.code == "malformed_json"


def test_honest_media_only_export_reporting(test_db: Database, media_only_bytes: bytes):
    importer = TelegramImporter(test_db)
    res = importer.import_chat_json(media_only_bytes)

    assert res.report.total_records == 3
    assert res.report.supported_text_messages == 0
    assert res.report.service_records == 1
    assert res.report.media_only_messages == 2
    assert res.report.stickers_count == 1
    assert res.report.unsupported_media_count == 2
    assert res.chat.supported_text_messages == 0


def test_import_name_override(test_db: Database, chat_a_bytes: bytes):
    importer = TelegramImporter(test_db)
    res = importer.import_chat_json(chat_a_bytes, name_override="Custom Overridden Name")
    assert res.chat.name == "Custom Overridden Name"


# R3: Strict ID validation and malformed record shape tests
def test_reject_malformed_record_shapes(test_db: Database):
    importer = TelegramImporter(test_db)
    valid_msg = {"id": 1, "text": "Hello", "date": "2024-01-01T12:00:00Z"}

    for bad_item in [17, "not-a-dict", [1, 2, 3], None, True]:
        export = {"name": "Test Chat", "messages": [valid_msg, bad_item]}
        payload = json.dumps(export).encode("utf-8")
        with pytest.raises(ImporterError) as exc_info:
            importer.import_chat_json(payload)
        assert exc_info.value.code == "malformed_record"
        assert "Record at index 1 must be an object" in exc_info.value.message

        # Ensure atomic rollback
        with test_db.connection() as conn:
            assert conn.execute("SELECT count(*) FROM chats;").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM messages;").fetchone()[0] == 0


def test_reject_invalid_source_message_ids(test_db: Database):
    importer = TelegramImporter(test_db)
    invalid_ids = [
        1.5,
        1.0,
        True,
        False,
        2**64,
        -(2**64),
        {"id": 1},
        [1],
        "1.5",
        "abc",
        "",
    ]
    for bad_id in invalid_ids:
        export = {
            "name": "Test Chat",
            "messages": [{"id": bad_id, "text": "msg", "date": "2024-01-01T12:00:00Z"}],
        }
        payload = json.dumps(export).encode("utf-8")
        with pytest.raises(ImporterError) as exc_info:
            importer.import_chat_json(payload)
        assert exc_info.value.code == "invalid_message_id"


def test_reject_invalid_reply_ids(test_db: Database):
    importer = TelegramImporter(test_db)
    invalid_reply_ids = [1.5, True, False, [1, 2], {"nested": 1}, 2**64, "1.5", "invalid"]
    for bad_reply in invalid_reply_ids:
        export = {
            "name": "Test Chat",
            "messages": [
                {
                    "id": 1,
                    "text": "msg",
                    "reply_to_message_id": bad_reply,
                    "date": "2024-01-01T12:00:00Z",
                }
            ],
        }
        payload = json.dumps(export).encode("utf-8")
        with pytest.raises(ImporterError) as exc_info:
            importer.import_chat_json(payload)
        assert exc_info.value.code == "invalid_reply_id"


def test_reject_malformed_text_entities(test_db: Database):
    importer = TelegramImporter(test_db)
    bad_texts = [
        [{"type": "bold", "text": 123}],  # Non-string text in entity
        [{"type": "bold", "text": ["nested"]}],
        [42],  # Non-string non-dict part
        {"type": "bold", "text": "bad"},  # Direct dict instead of list/str
    ]
    for bad_text in bad_texts:
        export = {
            "name": "Test Chat",
            "messages": [{"id": 1, "text": bad_text, "date": "2024-01-01T12:00:00Z"}],
        }
        payload = json.dumps(export).encode("utf-8")
        with pytest.raises(ImporterError) as exc_info:
            importer.import_chat_json(payload)
        assert exc_info.value.code in ("malformed_text_entity", "malformed_text")


def test_reject_malformed_sender_fields(test_db: Database):
    importer = TelegramImporter(test_db)
    export = {
        "name": "Test Chat",
        "messages": [
            {
                "id": 1,
                "from": {"object": "sender"},
                "text": "Hello",
                "date": "2024-01-01T12:00:00Z",
            }
        ],
    }
    payload = json.dumps(export).encode("utf-8")
    with pytest.raises(ImporterError) as exc_info:
        importer.import_chat_json(payload)
    assert exc_info.value.code == "malformed_field"


# R4: Text-bearing stickers and whitespace consistency
def test_text_bearing_sticker_is_supported_and_retains_metadata(test_db: Database):
    importer = TelegramImporter(test_db)
    export = {
        "name": "Sticker Chat",
        "messages": [
            {
                "id": 1,
                "type": "message",
                "date": "2024-01-01T12:00:00Z",
                "media_type": "sticker",
                "sticker_emoji": "🎉",
                "file": "stickers/yay.webp",
                "text": "Yay celebration!",
            }
        ],
    }
    payload = json.dumps(export).encode("utf-8")
    res = importer.import_chat_json(payload)

    # 1. Honest accounting in import report
    assert res.report.total_records == 1
    assert res.report.supported_text_messages == 1
    assert res.report.stickers_count == 1
    assert res.chat.supported_text_messages == 1

    # 2. Database verification: kind is sticker, is_supported_text is 1
    with test_db.connection() as conn:
        row = conn.execute(
            """
            SELECT kind, text, is_supported_text, sticker_metadata_json
            FROM messages WHERE chat_id = ? AND source_message_id = 1;
            """,
            (res.chat.id,),
        ).fetchone()
        assert row["kind"] == "sticker"
        assert row["text"] == "Yay celebration!"
        assert row["is_supported_text"] == 1
        meta = json.loads(row["sticker_metadata_json"])
        assert meta["sticker_emoji"] == "🎉"
        assert meta["file"] == "stickers/yay.webp"


def test_whitespace_only_handling_consistency(test_db: Database):
    importer = TelegramImporter(test_db)
    export = {
        "name": "Whitespace Chat",
        "messages": [
            {
                "id": 1,
                "text": "   \t\n  \r  ",
                "date": "2024-01-01T12:00:00Z",
            },
            {
                "id": 2,
                "text": "\u00a0\u2000\u200b\ufeff",  # Unicode whitespace and zero-width
                "date": "2024-01-01T12:01:00Z",
            },
            {
                "id": 3,
                "text": "Actual content",
                "date": "2024-01-01T12:02:00Z",
            },
        ],
    }
    payload = json.dumps(export).encode("utf-8")
    res = importer.import_chat_json(payload)

    assert res.report.total_records == 3
    assert res.report.supported_text_messages == 1
    assert res.report.empty_messages == 2
    assert res.chat.supported_text_messages == 1

    with test_db.connection() as conn:
        count = conn.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ? AND is_supported_text = 1;",
            (res.chat.id,),
        ).fetchone()[0]
        assert count == 1


# R5: Date normalization, epoch precedence, and unknown date policies
def test_mismatched_local_date_and_epoch(test_db: Database):
    importer = TelegramImporter(test_db)
    # date is local Kyiv time (UTC+2), date_unixtime is 1704110400 (which is 12:00:00 UTC)
    export = {
        "name": "Date Chat",
        "messages": [
            {
                "id": 1,
                "text": "Hello",
                "date": "2024-01-01T14:00:00",
                "date_unixtime": "1704110400",
            }
        ],
    }
    payload = json.dumps(export).encode("utf-8")
    res = importer.import_chat_json(payload)

    assert res.report.earliest_sent_at == "2024-01-01T12:00:00+00:00"
    assert res.report.latest_sent_at == "2024-01-01T12:00:00+00:00"

    with test_db.connection() as conn:
        row = conn.execute(
            "SELECT sent_at, raw_date FROM messages WHERE chat_id = ?;", (res.chat.id,)
        ).fetchone()
        assert row["sent_at"] == "2024-01-01T12:00:00+00:00"
        assert row["raw_date"] == "2024-01-01T14:00:00"


def test_offset_aware_and_naive_date_policies(test_db: Database):
    importer = TelegramImporter(test_db)
    export = {
        "name": "Date Variants",
        "messages": [
            # 1. Offset-aware ISO
            {
                "id": 1,
                "text": "Offset aware",
                "date": "2024-01-01T14:00:00+02:00",
            },
            # 2. Naive ISO date without timezone or epoch -> sent_at must be None
            {
                "id": 2,
                "text": "Naive without timezone",
                "date": "2026-01-01T12:00:00",
            },
            # 3. Missing date -> sent_at None, raw_date empty
            {
                "id": 3,
                "text": "Missing date",
            },
            # 4. Invalid date -> sent_at None, raw_date preserved
            {
                "id": 4,
                "text": "Invalid date",
                "date": "corrupted-date-string",
            },
        ],
    }
    payload = json.dumps(export).encode("utf-8")
    res = importer.import_chat_json(payload)

    # Only message 1 had a valid normalized instant
    assert res.report.earliest_sent_at == "2024-01-01T12:00:00+00:00"
    assert res.report.latest_sent_at == "2024-01-01T12:00:00+00:00"

    with test_db.connection() as conn:
        rows = {
            r["source_message_id"]: (r["sent_at"], r["raw_date"])
            for r in conn.execute(
                "SELECT source_message_id, sent_at, raw_date FROM messages WHERE chat_id = ?;",
                (res.chat.id,),
            ).fetchall()
        }
        # Message 1: offset converted to UTC
        assert rows[1] == ("2024-01-01T12:00:00+00:00", "2024-01-01T14:00:00+02:00")
        # Message 2: naive recorded as unknown (None), raw_date preserved
        assert rows[2] == (None, "2026-01-01T12:00:00")
        # Message 3: missing recorded as (None, "")
        assert rows[3] == (None, "")
        # Message 4: invalid recorded as (None, "corrupted-date-string")
        assert rows[4] == (None, "corrupted-date-string")


def test_naive_date_with_import_timezone(test_db: Database):
    importer = TelegramImporter(test_db)
    export = {
        "name": "Timezone Chat",
        "messages": [
            {
                "id": 1,
                "text": "Kyiv morning",
                "date": "2024-01-01T12:00:00",
            }
        ],
    }
    payload = json.dumps(export).encode("utf-8")
    res = importer.import_chat_json(payload, import_timezone="Europe/Kyiv")

    # Kyiv in January is UTC+2, so 12:00 local is 10:00 UTC
    assert res.report.earliest_sent_at == "2024-01-01T10:00:00+00:00"
    with test_db.connection() as conn:
        row = conn.execute(
            "SELECT sent_at, raw_date FROM messages WHERE chat_id = ?;", (res.chat.id,)
        ).fetchone()
        assert row["sent_at"] == "2024-01-01T10:00:00+00:00"
        assert row["raw_date"] == "2024-01-01T12:00:00"


# R3b: Top-level chat metadata shape validation and atomic DB non-modification
def test_reject_invalid_top_level_chat_metadata(test_db: Database):
    importer = TelegramImporter(test_db)

    # 1. Invalid top-level chat names
    bad_names = [
        {"nested": 1},
        [1, 2],
        True,
        False,
        123,
        1.5,
    ]
    for bad_name in bad_names:
        export = {"name": bad_name, "messages": []}
        payload = json.dumps(export).encode("utf-8")
        with pytest.raises(ImporterError) as exc_info:
            importer.import_chat_json(payload)
        assert exc_info.value.code == "invalid_chat_name"
        with test_db.connection() as conn:
            assert conn.execute("SELECT count(*) FROM chats;").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM messages;").fetchone()[0] == 0

    # 2. Invalid name_override
    bad_overrides = [
        {"nested": 1},
        [1, 2],
        True,
        False,
        "",
        "   ",
    ]
    for bad_override in bad_overrides:
        export = {"name": "Valid Name", "messages": []}
        payload = json.dumps(export).encode("utf-8")
        with pytest.raises(ImporterError) as exc_info:
            importer.import_chat_json(payload, name_override=bad_override)  # type: ignore[arg-type]
        assert exc_info.value.code == "invalid_chat_name"
        with test_db.connection() as conn:
            assert conn.execute("SELECT count(*) FROM chats;").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM messages;").fetchone()[0] == 0

    # 3. Invalid top-level source chat IDs
    bad_chat_ids = [
        {"nested": 1},
        [1, 2],
        True,
        False,
        1.5,
        "not-a-number",
        99999999999999999999999999,  # Exceeds 64-bit int
        -99999999999999999999999999,
    ]
    for bad_id in bad_chat_ids:
        export = {"id": bad_id, "name": "Valid Name", "messages": []}
        payload = json.dumps(export).encode("utf-8")
        with pytest.raises(ImporterError) as exc_info:
            importer.import_chat_json(payload)
        assert exc_info.value.code == "invalid_source_chat_id"
        with test_db.connection() as conn:
            assert conn.execute("SELECT count(*) FROM chats;").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM messages;").fetchone()[0] == 0


# R4b: Canonical supported-text predicate unit coverage
def test_shared_canonical_predicate_coverage():
    # 1. Unicode whitespace
    unicode_spaces = [
        "\u00a0",  # No-break space
        "\u3000",  # Ideographic space
        "\u2000",  # En quad
        "\u200a",  # Hair space
        "\u2028",  # Line separator
        "\u2029",  # Paragraph separator
        "\u202f",  # Narrow no-break space
        "\u205f",  # Medium mathematical space
        "  \u00a0  \u3000  ",
    ]
    for s in unicode_spaces:
        assert is_supported_text_content(s) is False
        assert is_supported_message("message", s) is False

    # 2. Zero-width content
    zero_width_chars = [
        "\u200b",  # Zero-width space
        "\ufeff",  # Zero-width no-break space (BOM)
        "\u200c",  # Zero-width non-joiner
        "\u200d",  # Zero-width joiner
        "\u200b\u200c\u200d\ufeff",
        "   \u200b   ",
        "\x0b",  # Vertical tab
        "\x0c",  # Form feed
        "\r\n\t ",
    ]
    for s in zero_width_chars:
        assert is_supported_text_content(s) is False
        assert is_supported_message("message", s) is False

    # 3. Ordinary text
    ordinary_texts = [
        "hello",
        "  hello world  ",
        "100% yes",
        "привіт світе",
        "🍕 pizza emoji",
    ]
    for s in ordinary_texts:
        assert is_supported_text_content(s) is True
        assert is_supported_message("message", s) is True

    # 4. Service messages (never supported, even with non-empty text)
    assert is_supported_message("service", "User joined group") is False
    assert is_supported_message("service", "Pinned message") is False
    assert is_supported_message("service", "") is False

    # 5. Text-bearing stickers vs media
    assert is_supported_message("sticker", "🍕") is True
    assert is_supported_message("sticker", "") is False
    assert is_supported_message("sticker", "\u200b") is False
    assert is_supported_message("media", "") is False
