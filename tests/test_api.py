"""Tests for FastAPI HTTP endpoints conforming to docs/contracts.md."""

import io
import json

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile

from app.config import settings
from app.db import MIGRATIONS, Database
from app.main import read_bounded_upload


def test_health_endpoint(client: TestClient):
    response = client.get("/api/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["version"] == "0.1.0"
    assert data["database_ready"] is True
    assert "provider_configured" in data
    assert "telegram_configured" in data
    # Ensure no secrets or tokens are leaked
    body_str = response.text
    assert "AIza" not in body_str
    assert "token" not in body_str.lower() or "telegram_configured" in body_str


def test_import_and_chat_crud(client: TestClient, chat_a_bytes: bytes):
    # 1. Import chat
    files = {"file": ("synthetic_chat_a.json", io.BytesIO(chat_a_bytes), "application/json")}
    res = client.post("/api/chats/import", files=files, data={"name": "API Imported Chat"})
    assert res.status_code == 200
    import_data = res.json()
    chat = import_data["chat"]
    report = import_data["report"]

    chat_id = chat["id"]
    assert chat["name"] == "API Imported Chat"
    assert report["total_records"] == 8
    assert report["supported_text_messages"] == 6

    # 2. List chats
    list_res = client.get("/api/chats")
    assert list_res.status_code == 200
    chats_list = list_res.json()
    assert chats_list["total"] == 1
    assert chats_list["items"][0]["id"] == chat_id
    assert chats_list["items"][0]["supported_text_messages"] == 6

    # 3. Get single chat
    get_res = client.get(f"/api/chats/{chat_id}")
    assert get_res.status_code == 200
    detail = get_res.json()
    assert detail["id"] == chat_id
    assert detail["coverage"]["import_coverage_percent"] == 100.0
    assert detail["supported_text_messages"] == 6

    # 4. Search memories
    mem_res = client.get(f"/api/chats/{chat_id}/memories", params={"q": "еспресо"})
    assert mem_res.status_code == 200
    memories = mem_res.json()
    assert memories["total"] == 1
    assert "подвійним еспресо" in memories["items"][0]["text"]
    assert memories["items"][0]["reply_parent"] is not None

    # 5. Delete chat
    del_res = client.delete(f"/api/chats/{chat_id}")
    assert del_res.status_code == 200
    assert del_res.json() == {"deleted": True, "id": chat_id}

    # Verify 404 after deletion
    get_again = client.get(f"/api/chats/{chat_id}")
    assert get_again.status_code == 404
    assert get_again.json()["error"]["code"] == "chat_not_found"


def test_import_idempotency_via_api(client: TestClient, chat_a_bytes: bytes):
    files1 = {"file": ("chat.json", io.BytesIO(chat_a_bytes), "application/json")}
    res1 = client.post("/api/chats/import", files=files1)
    assert res1.status_code == 200
    id1 = res1.json()["chat"]["id"]

    files2 = {"file": ("chat.json", io.BytesIO(chat_a_bytes), "application/json")}
    res2 = client.post("/api/chats/import", files=files2)
    assert res2.status_code == 200
    id2 = res2.json()["chat"]["id"]

    assert id1 == id2


def test_api_error_responses_standard_format(
    client: TestClient,
    account_export_bytes: bytes,
    duplicate_ids_bytes: bytes,
    malformed_bytes: bytes,
):
    # Nonexistent chat
    res_404 = client.get("/api/chats/nonexistent-id-12345")
    assert res_404.status_code == 404
    err_404 = res_404.json()
    assert "error" in err_404
    assert err_404["error"]["code"] == "chat_not_found"

    # Account export rejection
    files = {"file": ("acc.json", io.BytesIO(account_export_bytes), "application/json")}
    res_acc = client.post("/api/chats/import", files=files)
    assert res_acc.status_code == 400
    assert res_acc.json()["error"]["code"] == "account_export_rejected"

    # Duplicate IDs rejection
    files_dup = {"file": ("dup.json", io.BytesIO(duplicate_ids_bytes), "application/json")}
    res_dup = client.post("/api/chats/import", files=files_dup)
    assert res_dup.status_code == 400
    assert res_dup.json()["error"]["code"] == "duplicate_source_message_ids"

    # Malformed JSON rejection
    files_mal = {"file": ("mal.json", io.BytesIO(malformed_bytes), "application/json")}
    res_mal = client.post("/api/chats/import", files=files_mal)
    assert res_mal.status_code == 400
    assert res_mal.json()["error"]["code"] == "malformed_json"


def test_upload_size_limit_rejection(client: TestClient, monkeypatch):
    monkeypatch.setattr(settings, "max_import_size_bytes", 50)
    oversized = b"x" * 100
    files = {"file": ("big.json", io.BytesIO(oversized), "application/json")}
    res = client.post("/api/chats/import", files=files)
    assert res.status_code == 413
    assert res.json()["error"]["code"] == "payload_too_large"


# R6: Bounded upload read unit test
@pytest.mark.anyio
async def test_bounded_upload_stream_stops_early():
    """Verify that read_bounded_upload reads at most limit + 1 bytes and stops immediately."""

    class TrackingStream:
        def __init__(self, total_bytes: int):
            self.total_bytes = total_bytes
            self.bytes_read = 0
            self.closed = False

        def read(self, size: int = -1) -> bytes:
            if self.closed or self.bytes_read >= self.total_bytes:
                return b""
            chunk_size = (
                min(size, self.total_bytes - self.bytes_read)
                if size != -1
                else (self.total_bytes - self.bytes_read)
            )
            self.bytes_read += chunk_size
            return b"x" * chunk_size

        def close(self):
            self.closed = True

    # 10 MB stream
    stream = TrackingStream(total_bytes=10 * 1024 * 1024)
    upload_file = UploadFile(file=stream, filename="test.json")  # type: ignore[arg-type]

    limit = 100
    with pytest.raises(HTTPException) as exc_info:
        await read_bounded_upload(upload_file, max_bytes=limit)

    assert exc_info.value.status_code == 413
    assert exc_info.value.detail["code"] == "payload_too_large"
    # Stream read must stop at limit + 1 (101 bytes), NOT read the full 10 MB
    assert stream.bytes_read == limit + 1
    assert stream.closed is True


# R1: Exact parsed origin checks and rejection of remote lookalikes
def test_mutating_request_origin_boundary(client: TestClient, chat_a_bytes: bytes):
    # 1. Lookalike domain names and malformed origin values
    deceptive_origins = [
        "http://localhost.attacker.invalid:8000",
        "http://127.0.0.1.attacker.invalid:8000",
        "http://localhost.evil.com",
        "http://127.0.0.1.evil.com",
        "http://attacker-localhost:8000",
        "https://evil-attacker-website.com",
        "null",
        "http://user@127.0.0.1:8000",
        "http://user:pass@localhost:8000",
        "http://127.0.0.1:9999",  # Unexpected port
        "https://127.0.0.1:8000",  # Unexpected scheme
        "http://127.0.0.1:8000/extra/path",  # Invalid origin syntax
        "http://localhost:bad",  # R1b: non-integer port syntax
        "http://localhost:99999",  # R1b: out-of-range port
        "http://localhost:-1",  # R1b: negative port
    ]

    for bad_origin in deceptive_origins:
        res = client.post(
            "/api/chats/import",
            files={"file": ("chat.json", io.BytesIO(chat_a_bytes), "application/json")},
            headers={"origin": bad_origin},
        )
        assert res.status_code == 403, f"Origin {bad_origin} should have been rejected"
        assert res.json()["error"]["code"] == "forbidden_origin"

    # 2. Allowed exact local origins
    for good_origin in ["http://127.0.0.1:8000", "http://localhost:8000"]:
        res_good = client.post(
            "/api/chats/import",
            files={"file": ("chat.json", io.BytesIO(chat_a_bytes), "application/json")},
            headers={"origin": good_origin},
        )
        assert res_good.status_code == 200, f"Origin {good_origin} should have been accepted"

    # 3. Non-browser CLI requests without Origin header must remain usable
    res_no_origin = client.post(
        "/api/chats/import",
        files={"file": ("chat.json", io.BytesIO(chat_a_bytes), "application/json")},
    )
    assert res_no_origin.status_code == 200


def test_is_allowed_origin_direct():
    from app.main import is_allowed_origin

    # R1b: direct tests for malformed port parsing
    assert is_allowed_origin("http://localhost:bad") is False
    assert is_allowed_origin("http://localhost:99999") is False
    assert is_allowed_origin("http://localhost:-1") is False
    assert is_allowed_origin("http://localhost:abc") is False

    # Valid origins
    assert is_allowed_origin("http://localhost:8000") is True
    assert is_allowed_origin("http://127.0.0.1:8000") is True
    assert is_allowed_origin(None) is True

    # Bad schemes/hosts/null
    assert is_allowed_origin("https://localhost:8000") is False
    assert is_allowed_origin("http://attacker.com:8000") is False
    assert is_allowed_origin("null") is False
    assert is_allowed_origin("") is False


def test_import_invalid_metadata_rejected_with_atomic_db_preservation(
    client: TestClient, test_db: Database
):
    invalid_payloads = [
        ({"name": {"nested": 1}, "messages": []}, 400, "invalid_chat_name"),
        ({"name": [1, 2], "messages": []}, 400, "invalid_chat_name"),
        ({"name": True, "messages": []}, 400, "invalid_chat_name"),
        ({"id": {"nested": 1}, "messages": []}, 400, "invalid_source_chat_id"),
        ({"id": [1, 2], "messages": []}, 400, "invalid_source_chat_id"),
        ({"id": True, "messages": []}, 400, "invalid_source_chat_id"),
        ({"id": 1.5, "messages": []}, 400, "invalid_source_chat_id"),
    ]

    for payload, expected_status, expected_code in invalid_payloads:
        raw = json.dumps(payload).encode("utf-8")
        res = client.post(
            "/api/chats/import",
            files={"file": ("chat.json", io.BytesIO(raw), "application/json")},
        )
        assert res.status_code == expected_status
        assert res.json()["error"]["code"] == expected_code

        # Assert DB is unchanged (atomic non-modification)
        with test_db.connection() as conn:
            chat_count = conn.execute("SELECT count(*) FROM chats;").fetchone()[0]
            msg_count = conn.execute("SELECT count(*) FROM messages;").fetchone()[0]
            assert chat_count == 0
            assert msg_count == 0


def test_database_migration_v1_to_v2(tmp_path):
    """Test that existing local databases on schema v1 migrate to v3 without data loss."""
    db_file = tmp_path / "migration_test.db"
    db = Database(db_file)

    # 1. Bootstrap schema at version 1
    with db.connection() as conn:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )
        conn.executescript(MIGRATIONS[0][1])
        conn.execute("INSERT INTO schema_migrations VALUES (1, '2026-10-25T00:00:00Z');")

        # Insert chat and message under version 1
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES ('c1', 'Migrated Chat', 1, 'fp_migrated', '2026-10-25', '{}');
            """
        )
        conn.execute(
            """
            INSERT INTO messages (id, chat_id, source_message_id, sent_at, raw_date,
                                  kind, text, source_order)
            VALUES ('m1', 'c1', 1, '2026-10-25T12:00:00Z', '2026-10-25', 'message',
                    'Existing message before migration', 0);
            """
        )

    # 2. Run migrations to upgrade to latest
    db.run_migrations()

    # 3. Verify schema updates and integrity
    with db.connection() as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7]

        # Existing record preserved and is_supported_text populated
        row = conn.execute(
            "SELECT id, text, is_supported_text FROM messages WHERE id = 'm1';"
        ).fetchone()
        assert row["text"] == "Existing message before migration"
        assert row["is_supported_text"] == 1

        # Check sent_at is now nullable: inserting NULL sent_at succeeds
        conn.execute(
            """
            INSERT INTO messages (id, chat_id, source_message_id, sent_at, raw_date,
                                  kind, text, source_order, is_supported_text)
            VALUES ('m2', 'c1', 2, NULL, '2026-10-25', 'message',
                    'Message with null date', 1, 1);
            """
        )
        row2 = conn.execute("SELECT sent_at FROM messages WHERE id = 'm2';").fetchone()
        assert row2["sent_at"] is None

        # Verify FTS index remains intact and synchronized
        fts_rows = conn.execute(
            "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'migration';"
        ).fetchall()
        assert len(fts_rows) == 1
        assert fts_rows[0]["message_id"] == "m1"


def test_r3_api_budget_paused_resume_returns_400_not_404(client: TestClient, test_db: Database):
    """
    R3 regression:
    Resuming a budget-paused analysis on an existing valid chat without providing
    an explicit higher max_calls cap returns HTTP 400 budget_limit_reached,
    never HTTP 404 chat_not_found.
    """
    from app.main import app, get_ai_provider
    from app.providers.fake import FakeAIProvider
    from tests.test_analysis import _seed_chat_messages

    chat_id = "chat_api_budget_r3"
    _seed_chat_messages(test_db, chat_id, 10)

    fake_provider = FakeAIProvider()
    app.dependency_overrides[get_ai_provider] = lambda: fake_provider

    try:
        # 1. Start job with tight cap = 1 (will pause after 1 chunk)
        start_res = client.post(
            f"/api/chats/{chat_id}/analysis?wait=true",
            json={"max_calls": 1},
        )
        assert start_res.status_code == 200
        assert start_res.json()["status"] == "paused_budget"

        # 2. Resume without explicit higher budget ->
        # must return HTTP 400 budget_limit_reached (never 404)
        resume_res = client.post(
            f"/api/chats/{chat_id}/analysis?wait=true",
            json={},
        )
        assert resume_res.status_code == 400
        err = resume_res.json()
        assert err["error"]["code"] == "budget_limit_reached"
        assert "an explicit higher max_calls is required" in err["error"]["message"]

        # 3. Resume with explicit higher budget -> succeeds
        resume_res2 = client.post(
            f"/api/chats/{chat_id}/analysis?wait=true",
            json={"max_calls": 10},
        )
        assert resume_res2.status_code == 200
        assert resume_res2.json()["status"] == "completed"
    finally:
        app.dependency_overrides.clear()
