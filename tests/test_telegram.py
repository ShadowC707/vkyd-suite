"""Integration tests for Telegram polling, trigger parsing, dedupe, and group bindings."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from starlette.testclient import TestClient

from app.analysis import AnalysisService
from app.db import Database
from app.main import app
from app.providers.fake import FakeAIProvider
from app.replies import ReplyService
from app.retrieval import MemoryRetriever
from app.telegram import (
    FakeTelegramTransport,
    TelegramBotService,
    format_quote_attribution,
    parse_trigger,
)


def _seed_test_chat(db: Database, chat_id: str, count: int = 5) -> None:
    with db.transaction() as conn:
        stats = {
            "total_records": count,
            "supported_text_messages": count,
            "service_records": 0,
            "empty_messages": 0,
            "media_only_messages": 0,
            "unsupported_media_count": 0,
            "stickers_count": 0,
            "duplicate_records": 0,
            "earliest_sent_at": "2024-01-01T10:00:00Z",
            "latest_sent_at": "2024-01-01T10:05:00Z",
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES (?, 'Test Group Chat', 1, 'fp_tg', '2026-10-25', ?);
            """,
            (chat_id, json.dumps(stats)),
        )
        for i in range(1, count + 1):
            conn.execute(
                """
                INSERT OR REPLACE INTO messages (
                    id, chat_id, source_message_id, sender_name,
                    sent_at, raw_date, kind, text, source_order, is_supported_text
                ) VALUES (?, ?, ?, 'Bohdan', '2024-01-01T10:00:00Z', '2024-01-01',
                        'message', ?, ?, 1);
                """,
                (f"m_tg_{chat_id}_{i}", chat_id, i, f"Authentic joke {i} about coffee", i),
            )


@pytest.mark.anyio
async def test_telegram_trigger_parsing():
    """Verify trigger parsing for commands, mentions, direct replies, and non-triggers."""
    bot_id = 999
    bot_username = "test_bot"

    # 1. Non-triggered ordinary chat
    msg_normal = {"text": "Just talking about the weather"}
    is_trig, mode, _ = parse_trigger(msg_normal, bot_id, bot_username)
    assert is_trig is False

    # 2. Command /quote
    msg_quote = {"text": "/quote coffee"}
    is_trig, mode, ctx = parse_trigger(msg_quote, bot_id, bot_username)
    assert is_trig is True
    assert mode == "quote"
    assert ctx == "coffee"

    # 3. Command with @bot target matching this bot
    msg_quote_self = {"text": "/quote@test_bot coffee"}
    is_trig, mode, ctx = parse_trigger(msg_quote_self, bot_id, bot_username)
    assert is_trig is True
    assert mode == "quote"
    assert ctx == "coffee"

    # 4. Command targeted at a different bot
    msg_other_bot = {"text": "/quote@other_bot coffee"}
    is_trig, _, _ = parse_trigger(msg_other_bot, bot_id, bot_username)
    assert is_trig is False

    # 5. Mention @test_bot in sentence
    msg_mention = {"text": "Hey @test_bot what do you think?"}
    is_trig, mode, ctx = parse_trigger(msg_mention, bot_id, bot_username)
    assert is_trig is True
    assert mode == "auto"
    assert "@test_bot" not in ctx
    assert "what do you think?" in ctx

    # 6. Direct reply to this bot's previous message
    msg_reply = {
        "text": "Haha that was funny",
        "reply_to_message": {"from": {"id": 999, "is_bot": True}},
    }
    is_trig, mode, ctx = parse_trigger(msg_reply, bot_id, bot_username)
    assert is_trig is True
    assert mode == "auto"
    assert ctx == "Haha that was funny"

    # 7. System help command
    msg_help = {"text": "/help"}
    is_trig, mode, _ = parse_trigger(msg_help, bot_id, bot_username)
    assert is_trig is True
    assert mode == "system_help"


def test_oversized_quote_attribution_formatting():
    """Verify exact quote attribution formatting and safe length bounds."""
    # Normal short quote
    formatted = format_quote_attribution(
        text="Short punchy quote",
        sender_name="Oleksandr",
        sent_at="2024-02-15T12:00:00Z",
        source_message_id=42,
    )
    assert "«Short punchy quote»" in formatted
    assert "— Oleksandr, 15.02.2024, #42" in formatted

    # Extremely long quote (> 4000 characters)
    huge_text = "Word " * 1000  # 5,000 characters
    bounded = format_quote_attribution(
        text=huge_text,
        sender_name="Oleksandr",
        sent_at="2024-02-15T12:00:00Z",
        source_message_id=42,
        max_telegram_chars=4000,
    )
    assert len(bounded) <= 4000
    assert "— Oleksandr, 15.02.2024, #42" in bounded  # Attribution MUST remain intact!
    assert "[...]" in bounded


@pytest.mark.anyio
async def test_telegram_allowlist_and_binding_enforcement(tmp_path):
    """
    Verify Telegram adapter boundary:
    - Chat ID not in environment allowlist MUST NOT call provider or send message.
    - Chat ID in allowlist but unbound MUST NOT call provider or send message.
    - Only chat ID that is BOTH allowlisted and bound sends response.
    """
    db = Database(tmp_path / "tg_allow.db")
    db.run_migrations()

    chat_id = "chat_internal"
    _seed_test_chat(db, chat_id, 5)

    transport = FakeTelegramTransport()
    retriever = MemoryRetriever(db)
    analysis_service = AnalysisService(db)
    reply_service = ReplyService(db, retriever, analysis_service)
    provider = FakeAIProvider()

    # Allowlist includes only '-100111'
    allowed_groups = {"-100111"}
    bot = TelegramBotService(
        db=db,
        transport=transport,
        reply_service=reply_service,
        provider=provider,
        allowed_chat_ids=allowed_groups,
    )
    await bot.initialize()

    # Case 1: Message from disallowed chat (-100999)
    update_disallowed = {
        "update_id": 101,
        "message": {
            "message_id": 1,
            "chat": {"id": -100999},
            "text": "/quote coffee",
        },
    }
    res1 = await bot.process_update(update_disallowed)
    assert res1 is None
    assert len(transport.sent_messages) == 0

    # Case 2: Message from allowlisted chat (-100111), but UNBOUND
    update_unbound = {
        "update_id": 102,
        "message": {
            "message_id": 2,
            "chat": {"id": -100111},
            "text": "/quote coffee",
        },
    }
    res2 = await bot.process_update(update_unbound)
    assert res2 is None
    assert len(transport.sent_messages) == 0

    # Bind -100111 to chat_internal
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO telegram_bindings (
                telegram_chat_id, chat_id, enabled, cooldown_seconds, last_sent_at
            ) VALUES ('-100111', ?, 1, 10, NULL);
            """,
            (chat_id,),
        )

    # Case 3: Message from allowlisted AND bound chat
    update_bound = {
        "update_id": 103,
        "message": {
            "message_id": 3,
            "chat": {"id": -100111},
            "text": "/quote coffee",
        },
    }
    res3 = await bot.process_update(update_bound)
    assert res3 is not None
    assert len(transport.sent_messages) == 1
    sent = transport.sent_messages[0]
    assert sent["chat_id"] == "-100111"
    assert sent["reply_to_message_id"] == 3
    assert "Authentic joke" in sent["text"]


@pytest.mark.anyio
async def test_telegram_update_deduplication(tmp_path):
    """Verify duplicate update_ids are strictly processed once."""
    db = Database(tmp_path / "tg_dedupe.db")
    db.run_migrations()

    chat_id = "chat_dedupe"
    _seed_test_chat(db, chat_id, 3)

    transport = FakeTelegramTransport()
    retriever = MemoryRetriever(db)
    analysis_service = AnalysisService(db)
    reply_service = ReplyService(db, retriever, analysis_service)
    provider = FakeAIProvider()

    bot = TelegramBotService(
        db=db,
        transport=transport,
        reply_service=reply_service,
        provider=provider,
        allowed_chat_ids={"-100222"},
    )
    await bot.initialize()

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO telegram_bindings (telegram_chat_id, chat_id, enabled)
            VALUES ('-100222', ?, 1);
            """,
            (chat_id,),
        )

    dup_update = {
        "update_id": 500,
        "message": {
            "message_id": 10,
            "chat": {"id": -100222},
            "text": "/quote",
        },
    }

    # First delivery
    res_first = await bot.process_update(dup_update)
    assert res_first is not None
    assert len(transport.sent_messages) == 1

    # Second delivery with identical update_id
    res_second = await bot.process_update(dup_update)
    assert res_second is None
    # No duplicate message sent!
    assert len(transport.sent_messages) == 1


@pytest.mark.anyio
async def test_telegram_cooldown_and_pause_enforcement(tmp_path):
    """Verify cooldown timer and binding pause toggle prevent duplicate spam."""
    db = Database(tmp_path / "tg_cooldown.db")
    db.run_migrations()

    chat_id = "chat_cool"
    _seed_test_chat(db, chat_id, 3)

    transport = FakeTelegramTransport()
    retriever = MemoryRetriever(db)
    analysis_service = AnalysisService(db)
    reply_service = ReplyService(db, retriever, analysis_service)
    provider = FakeAIProvider()

    bot = TelegramBotService(
        db=db,
        transport=transport,
        reply_service=reply_service,
        provider=provider,
        allowed_chat_ids={"-100333"},
    )
    await bot.initialize()

    # Initial binding with 30s cooldown
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO telegram_bindings (
                telegram_chat_id, chat_id, enabled, cooldown_seconds, last_sent_at
            ) VALUES ('-100333', ?, 1, 30, NULL);
            """,
            (chat_id,),
        )

    # 1. First message succeeds
    upd1 = {
        "update_id": 601,
        "message": {"message_id": 21, "chat": {"id": -100333}, "text": "/quote"},
    }
    res1 = await bot.process_update(upd1)
    assert res1 is not None
    assert len(transport.sent_messages) == 1

    # 2. Immediate second message is blocked by cooldown
    upd2 = {
        "update_id": 602,
        "message": {"message_id": 22, "chat": {"id": -100333}, "text": "/quote"},
    }
    res2 = await bot.process_update(upd2)
    assert res2 is not None
    assert res2["action"] == "cooldown_skipped"
    assert len(transport.sent_messages) == 1  # No second message sent!

    # 3. Simulate cooldown expired
    old_time = (datetime.now(UTC) - timedelta(seconds=35)).isoformat()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE telegram_bindings SET last_sent_at = ? WHERE telegram_chat_id = '-100333';",
            (old_time,),
        )

    upd3 = {
        "update_id": 603,
        "message": {"message_id": 23, "chat": {"id": -100333}, "text": "/quote"},
    }
    res3 = await bot.process_update(upd3)
    assert res3 is not None
    assert len(transport.sent_messages) == 2

    # 4. Disable binding (pause)
    with db.transaction() as conn:
        conn.execute("UPDATE telegram_bindings SET enabled = 0 WHERE telegram_chat_id = '-100333';")

    upd4 = {
        "update_id": 604,
        "message": {"message_id": 24, "chat": {"id": -100333}, "text": "/quote"},
    }
    res4 = await bot.process_update(upd4)
    assert res4 is None
    assert len(transport.sent_messages) == 2


def test_telegram_api_status_and_binding_endpoints(tmp_path):
    """Verify HTTP API endpoints for telegram status and group binding CRUD."""
    db_file = tmp_path / "tg_api.db"
    db = Database(db_file)
    db.run_migrations()

    chat_id = "chat_api_binding"
    _seed_test_chat(db, chat_id, 2)

    from app.db import get_db

    app.dependency_overrides[get_db] = lambda: db

    try:
        client = TestClient(app)

        # 1. GET /api/telegram/status
        status_resp = client.get("/api/telegram/status")
        assert status_resp.status_code == 200
        data = status_resp.json()
        assert "configured" in data
        assert "allowed_chat_ids" in data
        assert isinstance(data["bindings"], list)

        # 2. PUT /api/telegram/bindings/{id} creates binding
        bind_resp = client.put(
            "/api/telegram/bindings/-100555",
            json={"chat_id": chat_id, "enabled": True, "cooldown_seconds": 15},
        )
        assert bind_resp.status_code == 200
        bind_data = bind_resp.json()
        assert bind_data["telegram_chat_id"] == "-100555"
        assert bind_data["chat_id"] == chat_id
        assert bind_data["enabled"] is True
        assert bind_data["cooldown_seconds"] == 15

        # 3. GET /api/telegram/status reflects newly added binding
        status_after = client.get("/api/telegram/status").json()
        assert any(b["telegram_chat_id"] == "-100555" for b in status_after["bindings"])

        # 4. Binding to non-existent chat returns 404
        err_resp = client.put(
            "/api/telegram/bindings/-100555",
            json={"chat_id": "non_existent_chat", "enabled": True},
        )
        assert err_resp.status_code == 404

        # 5. DELETE binding
        del_resp = client.delete("/api/telegram/bindings/-100555")
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] is True

        # Deleting non-existent returns 404
        del_404 = client.delete("/api/telegram/bindings/-100555")
        assert del_404.status_code == 404

    finally:
        app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_telegram_empty_allowlist_blocks_all_sends_even_with_active_binding(tmp_path):
    """
    S4 regression:
    Empty environment allowlist means no authorized groups, never unrestricted.
    A DB binding cannot bypass it: zero provider calls and zero sends.
    """
    db = Database(tmp_path / "tg_empty_allow.db")
    db.run_migrations()

    chat_id = "chat_empty_allow"
    _seed_test_chat(db, chat_id, 3)

    transport = FakeTelegramTransport()
    retriever = MemoryRetriever(db)
    analysis_service = AnalysisService(db)
    reply_service = ReplyService(db, retriever, analysis_service)
    provider = FakeAIProvider()

    # Empty allowlist
    bot = TelegramBotService(
        db=db,
        transport=transport,
        reply_service=reply_service,
        provider=provider,
        allowed_chat_ids=set(),
    )
    await bot.initialize()

    # Create active binding in DB for -100999
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO telegram_bindings (telegram_chat_id, chat_id, enabled)
            VALUES ('-100999', ?, 1);
            """,
            (chat_id,),
        )

    upd = {
        "update_id": 701,
        "message": {"message_id": 1, "chat": {"id": -100999}, "text": "/quote coffee"},
    }
    res = await bot.process_update(upd)
    assert res is None
    assert len(transport.sent_messages) == 0


@pytest.mark.anyio
async def test_telegram_command_addressed_to_other_bot_as_reply_is_rejected():
    """
    S4 regression:
    A command explicitly addressed to another bot (/quote@other_bot) sent as a reply
    to this bot MUST be rejected before reply/mention routing.
    """
    bot_id = 1234
    bot_username = "vkyd_bot"

    # Sent as a reply to this bot, but command targets @other_bot
    msg_other_bot_reply = {
        "text": "/quote@other_bot coffee",
        "reply_to_message": {"from": {"id": bot_id, "is_bot": True}},
    }
    is_trig, mode, _ = parse_trigger(msg_other_bot_reply, bot_id, bot_username)
    assert is_trig is False
    assert mode == ""

    # Also test /humor command support
    msg_humor = {"text": "/humor tell a joke"}
    is_trig_h, mode_h, ctx_h = parse_trigger(msg_humor, bot_id, bot_username)
    assert is_trig_h is True
    assert mode_h == "auto"
    assert "tell a joke" in ctx_h


@pytest.mark.anyio
async def test_actual_http_telegram_transport_serialization_with_mock_transport():
    """
    S4 regression:
    Actual HttpTelegramTransport through httpx.MockTransport serializes allowed_updates
    as a valid JSON-encoded array, and send_message uses official reply_parameters.
    """
    import httpx

    from app.telegram import HttpTelegramTransport

    captured_requests: list[httpx.Request] = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        path = request.url.path
        if "getMe" in path:
            return httpx.Response(
                200, json={"ok": True, "result": {"id": 123, "is_bot": True, "username": "mock"}}
            )
        if "getUpdates" in path:
            return httpx.Response(200, json={"ok": True, "result": []})
        if "sendMessage" in path:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 999}})
        if "sendSticker" in path:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1000}})
        return httpx.Response(404, json={"ok": False})

    mock_client = httpx.AsyncClient(
        transport=httpx.MockTransport(mock_handler),
        base_url="https://api.telegram.org/botDUMMY_TOKEN",
    )
    transport = HttpTelegramTransport(token="DUMMY_TOKEN")
    await transport.close()
    transport._client = mock_client

    # 1. Test get_updates serialization
    await transport.get_updates(offset=50, timeout=10)
    assert len(captured_requests) == 1
    req_updates = captured_requests[0]
    allowed_updates_param = req_updates.url.params.get("allowed_updates")
    assert allowed_updates_param is not None
    # Must decode cleanly with json.loads as a list!
    parsed_allowed = json.loads(allowed_updates_param)
    assert isinstance(parsed_allowed, list)
    assert parsed_allowed == ["message"]

    # 2. Test send_message with reply_parameters
    await transport.send_message(chat_id="-100123", text="Hello group", reply_to_message_id=77)
    assert len(captured_requests) == 2
    req_send = captured_requests[1]
    send_payload = json.loads(req_send.content)
    assert send_payload["chat_id"] == "-100123"
    assert send_payload["text"] == "Hello group"
    assert "reply_parameters" in send_payload
    assert send_payload["reply_parameters"] == {"message_id": 77}

    # 3. Test send_sticker with reply_parameters
    await transport.send_sticker(
        chat_id="-100123", sticker="sticker_file_id_123", reply_to_message_id=88
    )
    assert len(captured_requests) == 3
    req_sticker = captured_requests[2]
    sticker_payload = json.loads(req_sticker.content)
    assert sticker_payload["sticker"] == "sticker_file_id_123"
    assert sticker_payload["reply_parameters"] == {"message_id": 88}

    await transport.close()


def test_oversized_sender_name_in_quote_attribution():
    """
    S4 regression:
    format_quote_attribution with a 5000-character sender must not exceed max_telegram_chars.
    """
    res = format_quote_attribution(
        text="A quote",
        sender_name="S" * 5000,
        sent_at=None,
        source_message_id=1,
        max_telegram_chars=4000,
    )
    assert len(res) <= 4000
    assert "#1" in res
    assert "[...]" in res
    assert "«A quote»" in res


@pytest.mark.anyio
async def test_incoming_overlimit_context_handling_does_not_crash_or_dump_pydantic(tmp_path):
    """
    S4 regression:
    Incoming over-limit context (>4000 chars) is bounded safely without throwing
    raw Pydantic ValidationError or breaking the polling batch.
    """
    db = Database(tmp_path / "tg_overlimit.db")
    db.run_migrations()

    chat_id = "chat_overlimit"
    _seed_test_chat(db, chat_id, 3)

    transport = FakeTelegramTransport()
    retriever = MemoryRetriever(db)
    analysis_service = AnalysisService(db)
    reply_service = ReplyService(db, retriever, analysis_service)
    provider = FakeAIProvider()

    bot = TelegramBotService(
        db=db,
        transport=transport,
        reply_service=reply_service,
        provider=provider,
        allowed_chat_ids={"-100888"},
    )
    await bot.initialize()

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO telegram_bindings (telegram_chat_id, chat_id, enabled)
            VALUES ('-100888', ?, 1);
            """,
            (chat_id,),
        )

    # Over-limit text: 8,000 characters
    oversized_text = "Coffee context " * 600
    upd = {
        "update_id": 901,
        "message": {
            "message_id": 55,
            "chat": {"id": -100888},
            "text": f"/reply {oversized_text}",
        },
    }

    # Must process without throwing Pydantic validation error or crashing
    res = await bot.process_update(upd)
    assert res is not None
    assert len(transport.sent_messages) == 1
