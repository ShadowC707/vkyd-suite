"""Tests for evidence-backed group profile, versioned edits, and optimistic locking."""

from fastapi.testclient import TestClient

from app.db import Database
from app.main import app, get_ai_provider, get_db
from app.models import MemeFindingUpdate, ProfileUpdateRequest
from app.providers.fake import FakeAIProvider
from tests.test_analysis import _seed_chat_messages


def test_profile_lifecycle_and_optimistic_conflict(tmp_path):
    """Profile versioning creates new versions, and stale expected_version writes fail with 409."""
    db_file = tmp_path / "profile_test.db"
    db = Database(db_file)
    db.run_migrations()

    chat_id = "chat_prof_test"
    _seed_chat_messages(db, chat_id, 10)

    fake_provider = FakeAIProvider()
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_ai_provider] = lambda: fake_provider

    client = TestClient(app)

    # 1. Before analysis: 404 profile_not_found
    res_before = client.get(f"/api/chats/{chat_id}/profile")
    assert res_before.status_code == 404
    assert res_before.json()["error"]["code"] == "profile_not_found"

    # 2. Run analysis synchronously to produce version 1
    analysis_res = client.post(f"/api/chats/{chat_id}/analysis?wait=true")
    assert analysis_res.status_code == 200
    assert analysis_res.json()["status"] == "completed"

    # 3. Get profile version 1
    res_v1 = client.get(f"/api/chats/{chat_id}/profile")
    assert res_v1.status_code == 200
    p1 = res_v1.json()
    assert p1["version"] == 1
    assert p1["edited_by_user"] is False
    assert len(p1["memes"]) >= 1

    meme_id = p1["memes"][0]["id"]

    # 4. Update profile with expected_version = 1 -> creates version 2
    update_payload = ProfileUpdateRequest(
        expected_version=1,
        instructions_md="# Owner Edited Instructions\nAlways reply with irony.",
        structured={"custom_rule": "irony_only"},
        memes=[MemeFindingUpdate(id=meme_id, approval_state="rejected")],
    )
    res_update = client.put(f"/api/chats/{chat_id}/profile", json=update_payload.model_dump())
    assert res_update.status_code == 200
    p2 = res_update.json()
    assert p2["version"] == 2
    assert p2["edited_by_user"] is True
    assert p2["instructions_md"] == "# Owner Edited Instructions\nAlways reply with irony."
    # Meme approval state updated to rejected on the inherited snapshot
    assert len(p2["memes"]) == len(p1["memes"])
    updated_meme = p2["memes"][0]
    assert updated_meme["approval_state"] == "rejected"
    assert updated_meme["label"] == p1["memes"][0]["label"]
    assert updated_meme["id"] != meme_id  # Inherited meme gets fresh snapshot ID

    # R6 verification: Profile 1's historical snapshot remains completely intact in DB
    with db.connection() as conn:
        p1_row = conn.execute(
            "SELECT id FROM profiles WHERE chat_id = ? AND version = 1;", (chat_id,)
        ).fetchone()
        p1_memes = conn.execute(
            "SELECT * FROM memes WHERE profile_id = ?;", (p1_row["id"],)
        ).fetchall()
        assert len(p1_memes) == 1
        assert p1_memes[0]["id"] == meme_id
        assert p1_memes[0]["approval_state"] == "approved"  # Historical snapshot preserved

    # R6 verification: Submitting a stale/historical meme ID from v1 when updating v2 is rejected
    stale_meme_update = ProfileUpdateRequest(
        expected_version=2,
        instructions_md="# Owner Edited Instructions\nAlways reply with irony.",
        memes=[MemeFindingUpdate(id=meme_id, approval_state="approved")],
    )
    res_stale_meme = client.put(
        f"/api/chats/{chat_id}/profile", json=stale_meme_update.model_dump()
    )
    assert res_stale_meme.status_code == 400
    assert res_stale_meme.json()["error"]["code"] == "invalid_update"
    assert (
        "does not belong to expected profile version 2" in res_stale_meme.json()["error"]["message"]
    )

    # R5 verification: Rejecting oversized structured value (100k chars) with HTTP 422
    res_oversize = client.put(
        f"/api/chats/{chat_id}/profile",
        json={
            "expected_version": 2,
            "instructions_md": "# Test",
            "structured": {"huge": "x" * 100_000},
        },
    )
    assert res_oversize.status_code == 422

    # 5. Stale write with expected_version = 1 -> rejected with HTTP 409 conflict
    res_stale = client.put(f"/api/chats/{chat_id}/profile", json=update_payload.model_dump())
    assert res_stale.status_code == 409
    assert res_stale.json()["error"]["code"] == "version_conflict"

    # 6. Export profile markdown: contains instructions, no credentials, excludes rejected memes
    export_res = client.get(f"/api/chats/{chat_id}/profile/export")
    assert export_res.status_code == 200
    assert export_res.headers["content-type"].startswith("text/markdown")
    content = export_res.text
    assert "# Owner Edited Instructions" in content
    assert "Profile Version: 2" in content
    assert "Edited by Owner: Yes" in content
    assert "api_key" not in content.lower()
    assert "secret" not in content.lower()
    # Rejected meme must NOT appear in the approved memes section of export
    assert updated_meme["label"] not in content

    app.dependency_overrides.clear()


def test_historical_chat_memes_not_returned_as_current_profile_memes(tmp_path):
    """Historical memes from prior runs do not accumulate indefinitely into the active profile."""
    db_file = tmp_path / "memes_history.db"
    db = Database(db_file)
    db.run_migrations()

    chat_id = "chat_memes_history"
    _seed_chat_messages(db, chat_id, 10)

    from app.analysis import AnalysisService

    service = AnalysisService(db, chunk_size=5, overlap=0)
    provider = FakeAIProvider()

    # Run analysis 1 -> profile 1 with 1 meme
    j1 = service.create_or_resume_job(chat_id, max_calls=10)
    import asyncio

    asyncio.run(service.execute_job(chat_id, j1.id, provider))

    p1 = service.get_latest_profile(chat_id)
    assert p1 is not None
    assert p1.version == 1
    assert len(p1.memes) == 1

    # Now owner updates instructions (creates profile 2)
    service.update_profile(chat_id, 1, "# Owner Instructions V2")

    p2 = service.get_latest_profile(chat_id)
    assert p2 is not None
    assert p2.version == 2
    # Profile 2 has exactly 1 inherited meme, not duplicates!
    assert len(p2.memes) == 1
    assert p2.memes[0].chat_id == chat_id

    # Verify profile 1's historical snapshot meme remains in SQLite
    with db.connection() as conn:
        p1_row = conn.execute(
            "SELECT id FROM profiles WHERE chat_id = ? AND version = 1;", (chat_id,)
        ).fetchone()
        p1_memes = conn.execute(
            "SELECT id FROM memes WHERE profile_id = ?;", (p1_row["id"],)
        ).fetchall()
        assert len(p1_memes) == 1, "Profile 1 meme count must remain 1 (not drop to 0)"


def test_normalize_active_profiles_resolves_legacy_multi_active(tmp_path):
    """Multiple active profile flags are safely normalized to the latest version."""
    from app.db import normalize_active_profiles

    db = Database(tmp_path / "multi_active.db")
    db.run_migrations()

    chat_id = "chat_multi"
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES (?, 'Multi Chat', 1, 'fp_multi', '2026-10-25', '{}');
            """,
            (chat_id,),
        )
        conn.execute(
            """
            INSERT INTO profiles (id, chat_id, version, instructions_md,
                                 structured_json, is_active, created_at)
            VALUES ('p1', ?, 1, 'inst 1', '{}', 1, '2026-10-25T10:00:00Z');
            """,
            (chat_id,),
        )
        conn.execute(
            """
            INSERT INTO profiles (id, chat_id, version, instructions_md,
                                 structured_json, is_active, created_at)
            VALUES ('p2', ?, 2, 'inst 2', '{}', 1, '2026-10-25T11:00:00Z');
            """,
            (chat_id,),
        )

    with db.transaction() as conn:
        normalize_active_profiles(conn)

    with db.connection() as conn:
        rows = conn.execute(
            "SELECT id, version, is_active FROM profiles WHERE chat_id = ? ORDER BY version ASC;",
            (chat_id,),
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["version"] == 1 and rows[0]["is_active"] == 0
        assert rows[1]["version"] == 2 and rows[1]["is_active"] == 1
