"""Tests for database migrations (v1 upgrade, v2 repair, fresh DB, idempotency)."""

import json

from app.db import MIGRATIONS, Database


def test_fresh_db_migration(tmp_path):
    """A fresh database migrates through all versions to v3."""
    db_file = tmp_path / "fresh.db"
    db = Database(db_file)

    db.run_migrations()

    with db.connection() as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7]

        # Verify tables and triggers exist
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()
        }
        assert "chats" in tables
        assert "messages" in tables
        assert "messages_fts" in tables

    # Repeated migration run is idempotent
    db.run_migrations()
    with db.connection() as conn:
        versions2 = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions2 == [1, 2, 3, 4, 5, 6, 7]


def test_v1_upgrade_to_v3(tmp_path):
    """A v1 database upgrades to v3 with canonical predicates and refreshed report counts."""
    db_file = tmp_path / "v1_upgrade.db"
    db = Database(db_file)

    # 1. Bootstrap at v1
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

        # Initial import report before migration (v1 had no is_supported_text column)
        initial_stats = {
            "total_records": 7,
            "supported_text_messages": 5,  # Mismatched initial count
            "service_records": 1,
            "empty_messages": 0,
            "media_only_messages": 1,
            "unsupported_media_count": 0,
            "stickers_count": 2,
            "duplicate_records": 0,
            "earliest_sent_at": "2024-01-01T10:00:00+00:00",
            "latest_sent_at": "2024-01-01T11:00:00+00:00",
        }
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES ('c1', 'v1 Chat', 1, 'fp_v1', '2026-10-25', ?);
            """,
            (json.dumps(initial_stats),),
        )

        t0 = "2024-01-01T10:00:00+00:00"
        test_records = [
            ("m1", "c1", 1, t0, "2024-01-01", "message", "Normal text", None, 0),
            ("m2", "c1", 2, t0, "2024-01-01", "message", "\u200b", None, 1),
            ("m3", "c1", 3, t0, "2024-01-01", "message", "\u00a0", None, 2),
            ("m4", "c1", 4, t0, "2024-01-01", "message", "\x0b", None, 3),
            ("m5", "c1", 5, t0, "2024-01-01", "service", "Pinned msg", None, 4),
            ("m6", "c1", 6, t0, "2024-01-01", "sticker", "🍕", 1, 5),
            ("m7", "c1", 7, t0, "2024-01-01", "sticker", "", None, 6),
        ]
        conn.executemany(
            """
            INSERT INTO messages (
                id, chat_id, source_message_id, sent_at, raw_date,
                kind, text, reply_to_source_id, source_order
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            test_records,
        )

    # 2. Run migrations
    db.run_migrations()

    # 3. Assertions
    with db.connection() as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7]

        rows = {
            r["id"]: (r["text"], r["is_supported_text"], r["reply_to_source_id"])
            for r in conn.execute(
                """
                SELECT id, text, is_supported_text, reply_to_source_id
                FROM messages WHERE chat_id = 'c1';
                """
            ).fetchall()
        }

        # Exact text, identities and replies preserved
        assert rows["m1"][0] == "Normal text"
        assert rows["m1"][1] == 1
        assert rows["m2"][0] == "\u200b"
        assert rows["m2"][1] == 0  # Zero-width not supported
        assert rows["m3"][0] == "\u00a0"
        assert rows["m3"][1] == 0  # Unicode whitespace not supported
        assert rows["m4"][0] == "\x0b"
        assert rows["m4"][1] == 0  # Vertical tab not supported
        assert rows["m5"][0] == "Pinned msg"
        assert rows["m5"][1] == 0  # Service message not supported
        assert rows["m6"][0] == "🍕"
        assert rows["m6"][1] == 1  # Text-bearing sticker supported
        assert rows["m6"][2] == 1  # Reply preserved
        assert rows["m7"][0] == ""
        assert rows["m7"][1] == 0  # Sticker without text not supported

        # Refreshed report counts in chats
        chat_row = conn.execute("SELECT import_stats_json FROM chats WHERE id = 'c1';").fetchone()
        stats = json.loads(chat_row["import_stats_json"])
        assert stats["total_records"] == 7
        assert stats["supported_text_messages"] == 2  # m1 and m6
        assert stats["empty_messages"] == 3  # m2, m3, m4
        assert stats["service_records"] == 1  # m5
        assert stats["media_only_messages"] == 1  # m7
        assert stats["duplicate_records"] == 0
        assert stats["earliest_sent_at"] == "2024-01-01T10:00:00+00:00"
        assert stats["latest_sent_at"] == "2024-01-01T11:00:00+00:00"

        # FTS consistency
        fts_m1 = conn.execute(
            "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'Normal';"
        ).fetchall()
        assert len(fts_m1) == 1
        assert fts_m1[0]["message_id"] == "m1"


def test_applied_v2_db_repair_to_v3(tmp_path):
    """
    An already-migrated v2 database (where SQLite trim marked Unicode whitespace and
    zero-width characters as supported text) is safely repaired by v3.
    """
    db_file = tmp_path / "v2_repair.db"
    db = Database(db_file)

    # 1. Setup database simulating v2 schema created with the SQLite-trim predicate
    with db.connection() as conn:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )
        # Apply v1 DDL
        conn.executescript(MIGRATIONS[0][1])

        # Apply legacy v2 migration using SQLite trim
        legacy_v2_ddl = """
        PRAGMA foreign_keys = OFF;
        CREATE TABLE messages_v2 (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            source_message_id INTEGER NOT NULL,
            sender_id TEXT,
            sender_name TEXT,
            sent_at TEXT,
            raw_date TEXT NOT NULL DEFAULT '',
            kind TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            reply_to_source_id INTEGER,
            sticker_metadata_json TEXT,
            source_order INTEGER NOT NULL,
            is_supported_text INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
            UNIQUE (chat_id, source_message_id)
        );
        DROP TABLE messages;
        ALTER TABLE messages_v2 RENAME TO messages;
        CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, message_id, chat_id, text)
            VALUES (new.rowid, new.id, new.chat_id, new.text);
        END;
        CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.rowid;
        END;
        CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.rowid;
            INSERT INTO messages_fts(rowid, message_id, chat_id, text)
            VALUES (new.rowid, new.id, new.chat_id, new.text);
        END;
        PRAGMA foreign_keys = ON;
        """
        conn.executescript(legacy_v2_ddl)
        conn.execute("INSERT INTO schema_migrations VALUES (1, '2026-10-25T00:00:00Z');")
        conn.execute("INSERT INTO schema_migrations VALUES (2, '2026-10-25T00:01:00Z');")

        # Unrepaired stats from legacy run
        legacy_stats = {
            "total_records": 5,
            "supported_text_messages": 4,  # m1, m2, m3, m4 were all marked supported!
            "service_records": 0,
            "empty_messages": 0,
            "media_only_messages": 0,
            "unsupported_media_count": 0,
            "stickers_count": 1,
            "duplicate_records": 0,
            "earliest_sent_at": "2024-01-01T10:00:00+00:00",
            "latest_sent_at": "2024-01-01T11:00:00+00:00",
        }
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES ('c2', 'Legacy v2 Chat', 1, 'fp_v2', '2026-10-25', ?);
            """,
            (json.dumps(legacy_stats),),
        )

        # In legacy v2, SQLite trim misclassified m2 (\u200b), m3 (\u00a0), m4 (\x0b) as 1
        t0 = "2024-01-01T10:00:00Z"
        misclassified_records = [
            ("m1", "c2", 1, t0, "2024-01-01", "message", "Actual text", None, 0, 1),
            ("m2", "c2", 2, t0, "2024-01-01", "message", "\u200b", None, 1, 1),
            ("m3", "c2", 3, t0, "2024-01-01", "message", "\u00a0", None, 2, 1),
            ("m4", "c2", 4, t0, "2024-01-01", "message", "\x0b", None, 3, 1),
            ("m5", "c2", 5, t0, "2024-01-01", "sticker", "🍕", 1, 4, 1),
        ]
        conn.executemany(
            """
            INSERT INTO messages (
                id, chat_id, source_message_id, sent_at, raw_date,
                kind, text, reply_to_source_id, source_order, is_supported_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            misclassified_records,
        )

    # 2. Run additive migration (v3)
    db.run_migrations()

    # 3. Assertions
    with db.connection() as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7]

        rows = {
            r["id"]: (r["text"], r["is_supported_text"])
            for r in conn.execute(
                "SELECT id, text, is_supported_text FROM messages WHERE chat_id = 'c2';"
            ).fetchall()
        }

        # The three mismatches are now repaired
        assert rows["m1"][1] == 1
        assert rows["m2"][1] == 0
        assert rows["m3"][1] == 0
        assert rows["m4"][1] == 0
        assert rows["m5"][1] == 1

        # Raw text preserved exactly
        assert rows["m2"][0] == "\u200b"
        assert rows["m3"][0] == "\u00a0"
        assert rows["m4"][0] == "\x0b"

        # Report counts refreshed
        chat_row = conn.execute("SELECT import_stats_json FROM chats WHERE id = 'c2';").fetchone()
        stats = json.loads(chat_row["import_stats_json"])
        assert stats["total_records"] == 5
        assert stats["supported_text_messages"] == 2  # m1 and m5
        assert stats["empty_messages"] == 3  # m2, m3, m4
        assert stats["stickers_count"] == 1
        assert stats["earliest_sent_at"] == "2024-01-01T10:00:00+00:00"

        # FTS consistency
        fts_m1 = conn.execute(
            "SELECT message_id FROM messages_fts WHERE messages_fts MATCH 'Actual';"
        ).fetchall()
        assert len(fts_m1) == 1
        assert fts_m1[0]["message_id"] == "m1"

    # Repeated migration run is idempotent
    db.run_migrations()
    with db.connection() as conn:
        versions_again = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions_again == [1, 2, 3, 4, 5, 6, 7]


def test_applied_v3_db_repair_to_v4_native_empty_and_whitespace(tmp_path):
    """
    Focused regression for task 02 preflight:
    An already-migrated v3 database (where migration 3 omitted native kind='empty' rows
    and only counted unsupported kind='message' rows) is safely repaired by v4.
    Counts both native empty and reclassified whitespace consistently, preserves
    all other source statistics, and is strictly idempotent.
    """
    db_file = tmp_path / "v3_repair.db"
    db = Database(db_file)

    # 1. Setup database simulating v3 applied state
    with db.connection() as conn:
        conn.execute(
            """
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )
        # Apply v1 DDL
        conn.executescript(MIGRATIONS[0][1])

        # Apply v2 DDL
        conn.executescript(MIGRATIONS[1][1])

        # Record migrations 1, 2, 3 as already applied
        conn.execute("INSERT INTO schema_migrations VALUES (1, '2026-10-25T00:00:00Z');")
        conn.execute("INSERT INTO schema_migrations VALUES (2, '2026-10-25T00:01:00Z');")
        conn.execute("INSERT INTO schema_migrations VALUES (3, '2026-10-25T00:02:00Z');")

        # Flawed import stats where v3 previously omitted native empty (counted 1 instead of 2)
        flawed_stats = {
            "total_records": 3,
            "supported_text_messages": 1,
            "service_records": 0,
            "empty_messages": 1,  # Flawed: only counted m3, missed native empty m2!
            "media_only_messages": 0,
            "unsupported_media_count": 0,
            "stickers_count": 0,
            "duplicate_records": 0,
            "earliest_sent_at": "2024-01-01T10:00:00+00:00",
            "latest_sent_at": "2024-01-01T11:00:00+00:00",
        }
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES ('c3', 'Applied v3 Chat', 1, 'fp_v3', '2026-10-25', ?);
            """,
            (json.dumps(flawed_stats),),
        )

        t0 = "2024-01-01T10:00:00+00:00"
        records = [
            ("m1", "c3", 1, t0, "2024-01-01", "message", "Real message", None, 0, 1),
            ("m2", "c3", 2, t0, "2024-01-01", "empty", "", None, 1, 0),  # Native empty
            ("m3", "c3", 3, t0, "2024-01-01", "message", "  \n\t ", None, 2, 0),  # Whitespace
        ]
        conn.executemany(
            """
            INSERT INTO messages (
                id, chat_id, source_message_id, sent_at, raw_date,
                kind, text, reply_to_source_id, source_order, is_supported_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            records,
        )

    # 2. Run migrations — will run v4 repair
    db.run_migrations()

    # 3. Assertions
    with db.connection() as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7]

        chat_row = conn.execute("SELECT import_stats_json FROM chats WHERE id = 'c3';").fetchone()
        stats = json.loads(chat_row["import_stats_json"])

        # Both native empty (m2) and whitespace-only (m3) are now counted as empty_messages
        assert stats["empty_messages"] == 2
        assert stats["supported_text_messages"] == 1
        assert stats["total_records"] == 3
        assert stats["service_records"] == 0
        assert stats["media_only_messages"] == 0
        assert stats["duplicate_records"] == 0
        assert stats["earliest_sent_at"] == "2024-01-01T10:00:00+00:00"
        assert stats["latest_sent_at"] == "2024-01-01T11:00:00+00:00"

    # 4. Repeated run is idempotent
    db.run_migrations()
    with db.connection() as conn:
        versions2 = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions2 == [1, 2, 3, 4, 5, 6, 7]


def test_v5_upgrade_to_v6_correctness_guarantees(tmp_path):
    """A v5 database upgrades to v6:
    adds claimed_worker_id, payload_json, and normalizes active profiles.
    """
    db_file = tmp_path / "v5_upgrade.db"
    db = Database(db_file)

    # 1. Bootstrap through v5
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
        for ver, handler in MIGRATIONS[1:5]:
            if callable(handler):
                handler(conn)
            else:
                conn.executescript(handler)
            conn.execute(
                "INSERT INTO schema_migrations VALUES (?, '2026-10-25T00:00:00Z');",
                (ver,),
            )

        # Seed data before v6:
        # A chat with two profiles both having is_active = 1 (legacy anomaly)
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES ('c_v5', 'v5 Chat', 1, 'fp_v5', '2026-10-25', '{}');
            """
        )
        conn.execute(
            """
            INSERT INTO profiles (id, chat_id, version, instructions_md, structured_json,
                                 edited_by_user, is_active, created_at)
            VALUES ('p1_v5', 'c_v5', 1, 'v1', '{}', 0, 1, '2026-10-25T00:00:00Z'),
                   ('p2_v5', 'c_v5', 2, 'v2', '{}', 0, 1, '2026-10-25T00:01:00Z');
            """
        )
        # Seed an analysis job and chunk with payload stored in findings_json
        conn.execute(
            """
            INSERT INTO analysis_jobs (id, chat_id, corpus_version, status,
                                      total_supported_messages, max_calls, created_at, updated_at)
            VALUES ('j_v5', 'c_v5', 1, 'pending', 5, 10, '2026-10-25', '2026-10-25');
            """
        )
        sample_payload = json.dumps([{"source_message_id": 1, "text": "hello"}])
        conn.execute(
            """
            INSERT INTO analysis_chunks (id, job_id, ordinal, source_ids_json, context_ids_json,
                                        status, findings_json, attempts)
            VALUES ('chk_v5', 'j_v5', 0, '[1]', '[]', 'pending', ?, 0);
            """,
            (sample_payload,),
        )

    # 2. Run migrations (applies v6)
    db.run_migrations()

    with db.connection() as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7]

        chunk_cols = {
            r["name"] for r in conn.execute("PRAGMA table_info(analysis_chunks);").fetchall()
        }
        assert "claimed_worker_id" in chunk_cols
        assert "payload_json" in chunk_cols

        # Verify payload_json was backfilled and findings_json cleared for pending chunk
        chk = conn.execute(
            "SELECT payload_json, findings_json FROM analysis_chunks WHERE id = 'chk_v5';"
        ).fetchone()
        assert chk["payload_json"] == sample_payload
        assert chk["findings_json"] is None

        # Verify multiple-active profiles normalized: p2_v5 is active (1), p1_v5 is inactive (0)
        p1 = conn.execute("SELECT is_active FROM profiles WHERE id = 'p1_v5';").fetchone()
        p2 = conn.execute("SELECT is_active FROM profiles WHERE id = 'p2_v5';").fetchone()
        assert p1["is_active"] == 0
        assert p2["is_active"] == 1


def test_applied_v4_db_upgrades_to_v5_intelligence_guarantees(tmp_path):
    """A v4 database upgrades cleanly to v5 adding worker, merge nodes, is_active, and memes."""
    db_file = tmp_path / "v4_upgrade.db"
    db = Database(db_file)

    # 1. Bootstrap at v4
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
        conn.executescript(MIGRATIONS[1][1])
        conn.execute("INSERT INTO schema_migrations VALUES (1, '2026-10-25T00:00:00Z');")
        conn.execute("INSERT INTO schema_migrations VALUES (2, '2026-10-25T00:01:00Z');")
        conn.execute("INSERT INTO schema_migrations VALUES (3, '2026-10-25T00:02:00Z');")
        conn.execute("INSERT INTO schema_migrations VALUES (4, '2026-10-25T00:03:00Z');")

        # Seed profile and legacy meme without profile_id
        conn.execute(
            """
            INSERT INTO chats (
                id, name, corpus_version, import_fingerprint, created_at, import_stats_json
            ) VALUES ('c_v4', 'v4 Chat', 1, 'fp_v4', '2026-10-25', '{}');
            """
        )
        conn.execute(
            """
            INSERT INTO profiles (
                id, chat_id, version, source_job_id,
                instructions_md, structured_json, edited_by_user, created_at
            ) VALUES ('p_v4', 'c_v4', 1, 'j1', 'Instructions', '{}', 1, '2026-10-25T00:00:00Z');
            """
        )
        conn.execute(
            """
            INSERT INTO memes (
                id, chat_id, label, description, usage_context,
                evidence_message_ids, confidence, approval_state
            ) VALUES ('m_v4', 'c_v4', 'Old Meme', 'A joke', 'Context', '[]', 0.9, 'approved');
            """
        )

    # 2. Run migrations
    db.run_migrations()

    # 3. Assertions
    with db.connection() as conn:
        versions = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5, 6, 7]

        # Verify active_worker_id exists on analysis_jobs
        job_cols = {r["name"] for r in conn.execute("PRAGMA table_info(analysis_jobs);").fetchall()}
        assert "active_worker_id" in job_cols

        # Verify analysis_merge_nodes table exists
        merge_table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_merge_nodes';"
        ).fetchone()
        assert merge_table is not None

        # Verify is_active on profiles and value is 1
        prof = conn.execute("SELECT is_active FROM profiles WHERE id = 'p_v4';").fetchone()
        assert prof["is_active"] == 1

        # Verify meme was backfilled with profile_id
        meme = conn.execute("SELECT profile_id FROM memes WHERE id = 'm_v4';").fetchone()
        assert meme["profile_id"] == "p_v4"

    # Idempotent re-run
    db.run_migrations()
    with db.connection() as conn:
        versions2 = [
            r[0]
            for r in conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version;"
            ).fetchall()
        ]
        assert versions2 == [1, 2, 3, 4, 5, 6, 7]
