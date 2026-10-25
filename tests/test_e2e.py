"""End-to-end HTTP integration tests for the complete synthetic flow and static UI."""

import io
from pathlib import Path

from fastapi.testclient import TestClient

from app.db import Database
from app.main import app, get_ai_provider, get_db
from app.providers.fake import FakeAIProvider

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def test_static_assets_serving(tmp_path: Path):
    """Static assets (index.html, app.css, app.js) serve correctly with HTTP 200."""
    db_file = tmp_path / "e2e_static.db"
    db = Database(db_file)
    db.run_migrations()

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    # 1. Root index.html
    res_index = client.get("/")
    assert res_index.status_code == 200
    assert "text/html" in res_index.headers["content-type"]
    assert "Вкид" in res_index.text
    assert '<script src="/static/app.js">' in res_index.text
    assert '<link rel="stylesheet" href="/static/app.css">' in res_index.text

    # 2. Stylesheet
    res_css = client.get("/static/app.css")
    assert res_css.status_code == 200
    assert "text/css" in res_css.headers["content-type"]
    assert "--coral-accent" in res_css.text

    # 3. JavaScript
    res_js = client.get("/static/app.js")
    assert res_js.status_code == 200
    assert (
        "application/javascript" in res_js.headers["content-type"]
        or "text/javascript" in res_js.headers["content-type"]
    )
    assert "safeText" in res_js.text

    app.dependency_overrides.clear()


def test_full_synthetic_e2e_flow(tmp_path: Path):
    """Complete synthetic flow:

    Import -> Search -> Estimate -> Budget Pause & Resume -> Profile Edit & Conflict
    -> AI Suggestion Apply -> Grounded Reply -> Feedback -> Profile Export -> Telegram Bindings.
    """
    db_file = tmp_path / "e2e_flow.db"
    db = Database(db_file)
    db.run_migrations()

    fake_provider = FakeAIProvider(
        fixed_action="quote",
        fixed_quote_id=1,
    )

    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_ai_provider] = lambda: fake_provider
    client = TestClient(app)

    # 1. Import synthetic chat export
    chat_bytes = (FIXTURES_DIR / "synthetic_chat_a.json").read_bytes()
    files = {"file": ("result.json", io.BytesIO(chat_bytes), "application/json")}
    res_import = client.post(
        "/api/chats/import",
        files=files,
        data={"name": "End-to-End Chat Test"},
    )
    assert res_import.status_code == 200
    import_data = res_import.json()
    chat_id = import_data["chat"]["id"]
    assert import_data["chat"]["supported_text_messages"] > 0

    # 2. List chats and verify chat detail
    res_list = client.get("/api/chats")
    assert res_list.status_code == 200
    assert any(c["id"] == chat_id for c in res_list.json()["items"])

    res_detail = client.get(f"/api/chats/{chat_id}")
    assert res_detail.status_code == 200
    detail = res_detail.json()
    assert detail["name"] == "End-to-End Chat Test"
    assert detail["supported_text_messages"] == import_data["chat"]["supported_text_messages"]

    # 3. Paginated Memory Search
    res_search = client.get(f"/api/chats/{chat_id}/memories?q=&limit=5&offset=0")
    assert res_search.status_code == 200
    search_data = res_search.json()
    assert search_data["total"] >= 5
    assert len(search_data["items"]) == 5
    first_item = search_data["items"][0]
    assert "source_message_id" in first_item
    assert "text" in first_item
    assert "sender_name" in first_item

    # Search with specific query
    res_query = client.get(f"/api/chats/{chat_id}/memories?q=кава&limit=5&offset=0")
    assert res_query.status_code == 200

    # 4. Analysis preflight estimate
    res_est = client.post(f"/api/chats/{chat_id}/analysis/estimate")
    assert res_est.status_code == 200
    est = res_est.json()
    assert est["estimated_chunks"] > 0
    assert est["estimated_total_calls"] > 0

    # 5. Start analysis with budget cap = 1 to test pause
    res_start = client.post(
        f"/api/chats/{chat_id}/analysis?wait=true",
        json={"max_calls": 1},
    )
    assert res_start.status_code == 200
    job_step1 = res_start.json()
    job_id = job_step1["id"]
    assert job_step1["status"] in ("paused_budget", "paused", "completed")
    assert job_step1["provider_calls"] <= 1

    # 6. Resume analysis with expanded budget to completion
    res_resume = client.post(
        f"/api/chats/{chat_id}/analysis?wait=true",
        json={"max_calls": 25},
    )
    assert res_resume.status_code == 200
    job_step2 = res_resume.json()
    assert job_step2["status"] == "completed"
    assert job_step2["completed_chunks"] == job_step2["total_chunks"]

    # 7. Check Profile
    res_profile = client.get(f"/api/chats/{chat_id}/profile")
    assert res_profile.status_code == 200
    profile = res_profile.json()
    active_version = profile["version"]
    assert active_version >= 1
    assert "instructions_md" in profile

    # 8. Profile edit with optimistic locking
    new_instructions = "# Custom Style\nAlways make dry puns about coffee."
    res_edit = client.put(
        f"/api/chats/{chat_id}/profile",
        json={
            "expected_version": active_version,
            "instructions_md": new_instructions,
        },
    )
    assert res_edit.status_code == 200
    updated_profile = res_edit.json()
    assert updated_profile["version"] == active_version + 1
    assert updated_profile["instructions_md"] == new_instructions

    # Version conflict check
    res_conflict = client.put(
        f"/api/chats/{chat_id}/profile",
        json={
            "expected_version": active_version,  # Stale version
            "instructions_md": "# Attempt overwrite with stale version",
        },
    )
    assert res_conflict.status_code == 409
    assert res_conflict.json()["error"]["code"] == "version_conflict"

    # 9. Profile Markdown Export
    res_export = client.get(f"/api/chats/{chat_id}/profile/export")
    assert res_export.status_code == 200
    assert "text/markdown" in res_export.headers["content-type"]
    assert "Custom Style" in res_export.text

    # 10. AI Profile Suggestion inspect & apply flow
    # Seed a pending suggestion
    import uuid

    sugg_id = str(uuid.uuid4())
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO profiles (
                id, chat_id, version, source_job_id,
                instructions_md, structured_json, edited_by_user, is_active, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0, datetime('now'));
            """,
            (
                sugg_id,
                chat_id,
                updated_profile["version"] + 1,
                job_id,
                "# AI Proposed Instructions\nStay relaxed and friendly.",
                "{}",
            ),
        )

    # Inspect suggestion via API
    res_sugg = client.get(f"/api/chats/{chat_id}/profile/suggestion")
    assert res_sugg.status_code == 200
    sugg = res_sugg.json()
    assert sugg["id"] == sugg_id
    assert sugg["edited_by_user"] is False

    # Apply suggestion with conflict guard
    res_apply = client.post(
        f"/api/chats/{chat_id}/profile/apply-suggestion",
        json={
            "expected_active_version": updated_profile["version"],
            "suggestion_id": sugg_id,
        },
    )
    assert res_apply.status_code == 200
    applied = res_apply.json()
    assert applied["version"] > updated_profile["version"]
    assert applied["instructions_md"] == "# AI Proposed Instructions\nStay relaxed and friendly."
    assert applied["is_active"] is True

    # Suggestion is now cleared
    res_sugg_after = client.get(f"/api/chats/{chat_id}/profile/suggestion")
    assert res_sugg_after.status_code == 404

    # 11. Playground grounded reply
    # Quote mode (works offline without provider)
    res_reply_quote = client.post(
        f"/api/chats/{chat_id}/reply",
        json={
            "mode": "quote",
            "context": "кава",
        },
    )
    assert res_reply_quote.status_code == 200
    reply_q = res_reply_quote.json()
    assert reply_q["action"] in ("quote", "silent")

    # Auto mode with fake provider
    res_reply_auto = client.post(
        f"/api/chats/{chat_id}/reply",
        json={
            "mode": "auto",
            "context": "розкажи про проект",
        },
    )
    assert res_reply_auto.status_code == 200
    reply_a = res_reply_auto.json()
    reply_id = reply_a["id"]
    assert reply_id is not None

    # 12. Record feedback
    res_fb = client.post(
        f"/api/chats/{chat_id}/replies/{reply_id}/feedback",
        json={"verdict": "landed"},
    )
    assert res_fb.status_code == 200
    assert res_fb.json()["verdict"] == "landed"

    # 13. Telegram status & bindings
    res_tg_status = client.get("/api/telegram/status")
    assert res_tg_status.status_code == 200
    tg_stat = res_tg_status.json()
    assert "configured" in tg_stat
    assert "allowed_chat_ids" in tg_stat
    assert "bindings" in tg_stat

    # Create/update binding
    tg_group_id = "-10099887766"
    res_bind = client.put(
        f"/api/telegram/bindings/{tg_group_id}",
        json={
            "chat_id": chat_id,
            "cooldown_seconds": 15,
            "enabled": True,
        },
    )
    assert res_bind.status_code == 200
    bind_data = res_bind.json()
    assert bind_data["telegram_chat_id"] == tg_group_id
    assert bind_data["chat_id"] == chat_id
    assert bind_data["cooldown_seconds"] == 15
    assert bind_data["enabled"] is True

    # Delete binding
    res_del_bind = client.delete(f"/api/telegram/bindings/{tg_group_id}")
    assert res_del_bind.status_code == 200
    assert res_del_bind.json()["deleted"] is True

    # 14. Origin boundary check: untrusted external origin on mutating route is rejected
    res_forbidden = client.put(
        f"/api/telegram/bindings/{tg_group_id}",
        headers={"Origin": "http://evil-tracker.example.com"},
        json={
            "chat_id": chat_id,
            "cooldown_seconds": 15,
            "enabled": True,
        },
    )
    assert res_forbidden.status_code == 403
    assert res_forbidden.json()["error"]["code"] == "forbidden_origin"

    app.dependency_overrides.clear()


def test_cross_chat_isolation_and_switching(tmp_path: Path):
    """Memories and analysis state between two distinct chats remain completely isolated."""
    db_file = tmp_path / "e2e_isolation.db"
    db = Database(db_file)
    db.run_migrations()

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    # Import Chat A
    chat_a_bytes = (FIXTURES_DIR / "synthetic_chat_a.json").read_bytes()
    res_a = client.post(
        "/api/chats/import",
        files={"file": ("chat_a.json", io.BytesIO(chat_a_bytes), "application/json")},
        data={"name": "Chat A Alpha"},
    )
    chat_a_id = res_a.json()["chat"]["id"]

    # Import Chat B
    chat_b_bytes = (FIXTURES_DIR / "synthetic_chat_b.json").read_bytes()
    res_b = client.post(
        "/api/chats/import",
        files={"file": ("chat_b.json", io.BytesIO(chat_b_bytes), "application/json")},
        data={"name": "Chat B Beta"},
    )
    chat_b_id = res_b.json()["chat"]["id"]

    assert chat_a_id != chat_b_id

    # Retrieve memories from Chat A
    mem_a = client.get(f"/api/chats/{chat_a_id}/memories?limit=50").json()["items"]
    # Retrieve memories from Chat B
    mem_b = client.get(f"/api/chats/{chat_b_id}/memories?limit=50").json()["items"]

    assert len(mem_a) > 0
    assert len(mem_b) > 0

    # Verify no leak of Chat A messages in Chat B query
    for item_b in mem_b:
        assert item_b["chat_id"] == chat_b_id

    app.dependency_overrides.clear()


def test_analysis_never_starts_on_view_or_selection(tmp_path: Path):
    """Loading metadata, list, detail, memories, or preflight estimates never creates a job."""
    db_file = tmp_path / "e2e_no_auto_job.db"
    db = Database(db_file)
    db.run_migrations()

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    chat_bytes = (FIXTURES_DIR / "synthetic_chat_a.json").read_bytes()
    res_import = client.post(
        "/api/chats/import",
        files={"file": ("result.json", io.BytesIO(chat_bytes), "application/json")},
        data={"name": "No Auto Job Chat"},
    )
    chat_id = res_import.json()["chat"]["id"]

    # View actions
    client.get("/api/chats")
    client.get(f"/api/chats/{chat_id}")
    client.get(f"/api/chats/{chat_id}/memories?limit=20")
    client.post(f"/api/chats/{chat_id}/analysis/estimate")

    # Assert 0 analysis jobs exist
    with db.connection() as conn:
        job_count = conn.execute("SELECT count(*) FROM analysis_jobs;").fetchone()[0]
        assert job_count == 0

    app.dependency_overrides.clear()


def test_untrusted_html_injection_preserved_accurately(tmp_path: Path):
    """Malicious scripts or tags in chat messages are preserved verbatim without execution."""
    db_file = tmp_path / "e2e_xss.db"
    db = Database(db_file)
    db.run_migrations()

    app.dependency_overrides[get_db] = lambda: db
    client = TestClient(app)

    malicious_json = """{
      "name": "<script>alert('xss-chat')</script>",
      "type": "public_supergroup",
      "id": 888777,
      "messages": [
        {
          "id": 101,
          "type": "message",
          "date": "2026-03-01T12:00:00",
          "date_unixtime": "1772366400",
          "from": "<img src=x onerror=alert(1)>",
          "from_id": "user101",
          "text": "<style>body{display:none}</style><b>Bold</b> <script>steal()</script>"
        }
      ]
    }"""

    res_import = client.post(
        "/api/chats/import",
        files={
            "file": (
                "xss.json",
                io.BytesIO(malicious_json.encode("utf-8")),
                "application/json",
            )
        },
    )
    assert res_import.status_code == 200
    chat_id = res_import.json()["chat"]["id"]

    # Retrieve memories
    res_mem = client.get(f"/api/chats/{chat_id}/memories")
    assert res_mem.status_code == 200
    items = res_mem.json()["items"]
    assert len(items) == 1
    expected_text = "<style>body{display:none}</style><b>Bold</b> <script>steal()</script>"
    assert items[0]["text"] == expected_text
    assert items[0]["sender_name"] == "<img src=x onerror=alert(1)>"

    app.dependency_overrides.clear()
