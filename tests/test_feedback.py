"""Tests for feedback persistence, chat isolation, and bounded ranking impact."""

from fastapi.testclient import TestClient

from app.db import Database
from app.main import app, get_ai_provider, get_db
from app.models import FeedbackRequest, ReplyRequest
from app.providers.fake import FakeAIProvider
from tests.test_analysis import _seed_chat_messages


def test_feedback_persistence_and_repeat_handling(tmp_path):
    """Submitting feedback updates the existing record rather than compounding duplicate entries."""
    db_file = tmp_path / "feedback_test.db"
    db = Database(db_file)
    db.run_migrations()

    chat_id = "chat_fb"
    _seed_chat_messages(db, chat_id, 5)

    fake_provider = FakeAIProvider()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_ai_provider] = lambda: fake_provider

    client = TestClient(app)

    # 1. Create a reply
    req = ReplyRequest(context="Message content number 1", mode="quote")
    reply_res = client.post(f"/api/chats/{chat_id}/reply", json=req.model_dump())
    assert reply_res.status_code == 200
    reply_id = reply_res.json()["id"]

    # 2. Record feedback 'landed'
    fb1_req = FeedbackRequest(verdict="landed")
    fb1_res = client.post(
        f"/api/chats/{chat_id}/replies/{reply_id}/feedback", json=fb1_req.model_dump()
    )
    assert fb1_res.status_code == 200
    assert fb1_res.json()["verdict"] == "landed"

    # Verify 1 entry in DB
    with db.connection() as conn:
        count = conn.execute(
            "SELECT count(*) FROM feedback WHERE reply_id = ?;", (reply_id,)
        ).fetchone()[0]
        assert count == 1

    # 3. Repeat feedback for same reply: update to 'overused'
    fb2_req = FeedbackRequest(verdict="overused")
    fb2_res = client.post(
        f"/api/chats/{chat_id}/replies/{reply_id}/feedback", json=fb2_req.model_dump()
    )
    assert fb2_res.status_code == 200
    assert fb2_res.json()["verdict"] == "overused"

    # Verify still exactly 1 entry in DB, updated in place
    with db.connection() as conn:
        rows = conn.execute(
            "SELECT verdict FROM feedback WHERE reply_id = ?;", (reply_id,)
        ).fetchall()
        assert len(rows) == 1
        assert rows[0]["verdict"] == "overused"

    app.dependency_overrides.clear()


def test_feedback_chat_isolation(tmp_path):
    """Feedback submitted to a mismatched chat is rejected with 404."""
    db_file = tmp_path / "feedback_iso.db"
    db = Database(db_file)
    db.run_migrations()

    chat_a = "chat_a"
    chat_b = "chat_b"
    _seed_chat_messages(db, chat_a, 3)
    _seed_chat_messages(db, chat_b, 3)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_ai_provider] = lambda: None

    client = TestClient(app)

    # Create reply in chat A
    req = ReplyRequest(context="Message content number 1", mode="quote")
    reply_res = client.post(f"/api/chats/{chat_a}/reply", json=req.model_dump())
    assert reply_res.status_code == 200
    reply_id = reply_res.json()["id"]

    # Attempt to post feedback for reply in chat B
    fb_req = FeedbackRequest(verdict="landed")
    res_b = client.post(
        f"/api/chats/{chat_b}/replies/{reply_id}/feedback", json=fb_req.model_dump()
    )
    assert res_b.status_code == 404
    assert res_b.json()["error"]["code"] == "reply_not_found"

    app.dependency_overrides.clear()


def test_feedback_adjusts_quote_ranking_deterministically(tmp_path):
    """Landed feedback boosts quote ranking while missed/overused penalizes ranking."""
    db_file = tmp_path / "feedback_ranking.db"
    db = Database(db_file)
    db.run_migrations()

    chat_id = "chat_ranking"
    _seed_chat_messages(db, chat_id, 10)

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_ai_provider] = lambda: None

    client = TestClient(app)

    # Reply for message 1
    req1 = ReplyRequest(context="Message content number 1", mode="quote")
    rep1_res = client.post(f"/api/chats/{chat_id}/reply", json=req1.model_dump())
    rep1_id = rep1_res.json()["id"]

    # Reply for message 2
    req2 = ReplyRequest(context="Message content number 2", mode="quote")
    rep2_res = client.post(f"/api/chats/{chat_id}/reply", json=req2.model_dump())
    rep2_id = rep2_res.json()["id"]

    # Rate reply 1 as 'landed' (+boost)
    client.post(f"/api/chats/{chat_id}/replies/{rep1_id}/feedback", json={"verdict": "landed"})

    # Rate reply 2 as 'overused' (-penalty)
    client.post(f"/api/chats/{chat_id}/replies/{rep2_id}/feedback", json={"verdict": "overused"})

    # Generic query matching both "Message content number"
    generic_req = ReplyRequest(context="Message content number", mode="quote")
    next_reply = client.post(f"/api/chats/{chat_id}/reply", json=generic_req.model_dump())
    assert next_reply.status_code == 200

    # Message 1 should be selected over message 2 due to landed boost vs overused penalty
    assert next_reply.json()["quote"]["source_message_id"] == 1

    app.dependency_overrides.clear()
