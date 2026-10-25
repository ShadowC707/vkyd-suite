"""Database connection management, migrations, and transactional execution."""

import json
import sqlite3
from collections.abc import Callable, Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.config import settings
from app.predicates import is_supported_message

MIGRATIONS: list[tuple[int, str | Callable[[sqlite3.Connection], None]]] = [
    (
        1,
        """
        -- Core chat entities
        CREATE TABLE IF NOT EXISTS chats (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            source_chat_id TEXT,
            corpus_version INTEGER NOT NULL DEFAULT 1,
            import_fingerprint TEXT UNIQUE NOT NULL,
            created_at TEXT NOT NULL,
            import_stats_json TEXT NOT NULL
        );

        -- Normalized messages
        CREATE TABLE IF NOT EXISTS messages (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            source_message_id INTEGER NOT NULL,
            sender_id TEXT,
            sender_name TEXT,
            sent_at TEXT NOT NULL,
            raw_date TEXT NOT NULL,
            kind TEXT NOT NULL,
            text TEXT NOT NULL DEFAULT '',
            reply_to_source_id INTEGER,
            sticker_metadata_json TEXT,
            source_order INTEGER NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
            UNIQUE (chat_id, source_message_id)
        );

        CREATE INDEX IF NOT EXISTS idx_messages_chat_order ON messages (chat_id, source_order);
        CREATE INDEX IF NOT EXISTS idx_messages_chat_reply
        ON messages (chat_id, reply_to_source_id);

        -- FTS5 search index
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            message_id UNINDEXED,
            chat_id UNINDEXED,
            text,
            tokenize='unicode61'
        );

        -- FTS synchronization triggers
        CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, message_id, chat_id, text)
            VALUES (new.rowid, new.id, new.chat_id, new.text);
        END;

        CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.rowid;
        END;

        CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
            DELETE FROM messages_fts WHERE rowid = old.rowid;
            INSERT INTO messages_fts(rowid, message_id, chat_id, text)
            VALUES (new.rowid, new.id, new.chat_id, new.text);
        END;

        -- Analysis tracking tables (for later slices)
        CREATE TABLE IF NOT EXISTS analysis_jobs (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            corpus_version INTEGER NOT NULL,
            status TEXT NOT NULL,
            total_supported_messages INTEGER NOT NULL DEFAULT 0,
            processed_unique_messages INTEGER NOT NULL DEFAULT 0,
            total_chunks INTEGER NOT NULL DEFAULT 0,
            completed_chunks INTEGER NOT NULL DEFAULT 0,
            provider_calls INTEGER NOT NULL DEFAULT 0,
            max_calls INTEGER NOT NULL DEFAULT 50,
            last_error_code TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS analysis_chunks (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            source_ids_json TEXT NOT NULL,
            context_ids_json TEXT NOT NULL,
            status TEXT NOT NULL,
            findings_json TEXT,
            attempts INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (job_id) REFERENCES analysis_jobs(id) ON DELETE CASCADE
        );

        -- Profiles and memes
        CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            version INTEGER NOT NULL,
            source_job_id TEXT,
            instructions_md TEXT NOT NULL,
            structured_json TEXT NOT NULL,
            edited_by_user INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS memes (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            label TEXT NOT NULL,
            description TEXT NOT NULL,
            usage_context TEXT,
            evidence_message_ids TEXT NOT NULL,
            confidence REAL,
            approval_state TEXT NOT NULL DEFAULT 'approved',
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );

        -- Replies and feedback
        CREATE TABLE IF NOT EXISTS replies (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            mode TEXT NOT NULL,
            action TEXT NOT NULL,
            text TEXT,
            quote_id TEXT,
            sticker_id TEXT,
            evidence_ids_json TEXT,
            provider_kind TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS feedback (
            id TEXT PRIMARY KEY,
            reply_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            verdict TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE,
            FOREIGN KEY (reply_id) REFERENCES replies(id) ON DELETE CASCADE
        );

        -- Telegram worker bindings and assets
        CREATE TABLE IF NOT EXISTS telegram_bindings (
            telegram_chat_id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            cooldown_seconds INTEGER NOT NULL DEFAULT 10,
            last_sent_at TEXT,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS telegram_updates (
            update_id INTEGER PRIMARY KEY,
            telegram_chat_id TEXT,
            processed_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS stickers (
            id TEXT PRIMARY KEY,
            chat_id TEXT NOT NULL,
            description TEXT,
            tags TEXT,
            file_unique_id TEXT,
            bot_id TEXT,
            file_id TEXT,
            export_metadata_json TEXT,
            FOREIGN KEY (chat_id) REFERENCES chats(id) ON DELETE CASCADE
        );
        """,
    ),
    (
        2,
        """
        -- Migration 2: Nullable sent_at and explicit is_supported_text flag
        PRAGMA foreign_keys = OFF;

        CREATE TABLE IF NOT EXISTS messages_v2 (
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

        INSERT INTO messages_v2 (
            rowid, id, chat_id, source_message_id, sender_id, sender_name,
            sent_at, raw_date, kind, text, reply_to_source_id,
            sticker_metadata_json, source_order, is_supported_text
        )
        SELECT
            rowid, id, chat_id, source_message_id, sender_id, sender_name,
            sent_at, raw_date, kind, text, reply_to_source_id,
            sticker_metadata_json, source_order,
            py_is_supported_text(kind, text)
        FROM messages;

        DROP TABLE messages;
        ALTER TABLE messages_v2 RENAME TO messages;

        CREATE INDEX IF NOT EXISTS idx_messages_chat_order ON messages (chat_id, source_order);
        CREATE INDEX IF NOT EXISTS idx_messages_chat_reply
            ON messages (chat_id, reply_to_source_id);
        CREATE INDEX IF NOT EXISTS idx_messages_chat_supported
            ON messages (chat_id, is_supported_text);

        DROP TRIGGER IF EXISTS messages_ai;
        DROP TRIGGER IF EXISTS messages_ad;
        DROP TRIGGER IF EXISTS messages_au;

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
        """,
    ),
    (
        3,
        lambda conn: migration_3_repair_supported_text(conn),
    ),
    (
        4,
        lambda conn: migration_4_repair_empty_messages_counter(conn),
    ),
    (
        5,
        lambda conn: migration_5_intelligence_guarantees(conn),
    ),
    (
        6,
        lambda conn: migration_6_runtime_correctness_repairs(conn),
    ),
    (
        7,
        lambda conn: migration_7_configurable_chunk_plan(conn),
    ),
]


def repair_chat_import_stats(conn: sqlite3.Connection) -> None:
    """
    Refreshes chats' import_stats_json counts (supported_text_messages, empty_messages,
    media_only_messages, service_records) to match actual database row counts.
    Consistently counts both native empty messages (kind='empty') and
    reclassified whitespace records (kind='message' AND is_supported_text=0) as empty_messages.
    Preserves total_records, earliest/latest timestamps, and all other user data.
    """
    chats = conn.execute("SELECT id, import_stats_json FROM chats;").fetchall()
    for chat in chats:
        chat_id = chat["id"]
        stats_raw = chat["import_stats_json"]
        if not stats_raw:
            continue
        try:
            stats = json.loads(stats_raw)
        except Exception:
            continue

        actual_supp = conn.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ? AND is_supported_text = 1;",
            (chat_id,),
        ).fetchone()[0]

        actual_empty = conn.execute(
            """
            SELECT count(*) FROM messages
            WHERE chat_id = ? AND (kind = 'empty' OR (kind = 'message' AND is_supported_text = 0));
            """,
            (chat_id,),
        ).fetchone()[0]

        actual_media_only = conn.execute(
            """
            SELECT count(*) FROM messages
            WHERE chat_id = ? AND (kind = 'media' OR (kind = 'sticker' AND is_supported_text = 0));
            """,
            (chat_id,),
        ).fetchone()[0]

        actual_service = conn.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ? AND kind = 'service';",
            (chat_id,),
        ).fetchone()[0]

        if (
            stats.get("supported_text_messages") != actual_supp
            or stats.get("empty_messages") != actual_empty
            or stats.get("media_only_messages") != actual_media_only
            or stats.get("service_records") != actual_service
        ):
            stats["supported_text_messages"] = actual_supp
            stats["empty_messages"] = actual_empty
            stats["media_only_messages"] = actual_media_only
            stats["service_records"] = actual_service
            conn.execute(
                "UPDATE chats SET import_stats_json = ? WHERE id = ?;",
                (json.dumps(stats, ensure_ascii=False), chat_id),
            )


def migration_3_repair_supported_text(conn: sqlite3.Connection) -> None:
    """
    Additive repair migration (v3):
    1. Repairs messages.is_supported_text using the canonical predicate
       (fixing records with Unicode whitespace, zero-width formatting, and vertical tabs
       that were misclassified by earlier v2 runs using SQLite trim).
       Preserves rowids, raw text, sender identities, and reply references.
       FTS triggers automatically synchronize messages_fts.
    2. Refreshes affected chats' import_stats_json counts via repair_chat_import_stats.
    """
    conn.execute(
        """
        UPDATE messages
        SET is_supported_text = py_is_supported_text(kind, text)
        WHERE is_supported_text != py_is_supported_text(kind, text);
        """
    )
    repair_chat_import_stats(conn)


def migration_4_repair_empty_messages_counter(conn: sqlite3.Connection) -> None:
    """
    Additive repair migration (v4):
    Refreshes chats' import_stats_json empty_messages counter to consistently count
    both native empty records (kind='empty') and reclassified whitespace records
    (kind='message' AND is_supported_text=0) for databases that already applied v3.
    """
    repair_chat_import_stats(conn)


def migration_5_intelligence_guarantees(conn: sqlite3.Connection) -> None:
    """
    Additive migration (v5):
    1. analysis_jobs: adds active_worker_id column for atomic single-worker ownership.
    2. analysis_merge_nodes: table for intermediate merge tree nodes for resumable hierarchy.
    3. profiles: adds is_active column and unique index on (chat_id, version).
    4. memes: adds profile_id column, backfills unassigned memes to their chat's latest profile,
       and adds index on memes(profile_id).
    """
    job_cols = {r["name"] for r in conn.execute("PRAGMA table_info(analysis_jobs);").fetchall()}
    if "active_worker_id" not in job_cols:
        conn.execute("ALTER TABLE analysis_jobs ADD COLUMN active_worker_id TEXT;")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analysis_merge_nodes (
            id TEXT PRIMARY KEY,
            job_id TEXT NOT NULL,
            level INTEGER NOT NULL,
            node_index INTEGER NOT NULL,
            status TEXT NOT NULL,
            findings_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (job_id) REFERENCES analysis_jobs(id) ON DELETE CASCADE,
            UNIQUE (job_id, level, node_index)
        );
        """
    )

    prof_cols = {r["name"] for r in conn.execute("PRAGMA table_info(profiles);").fetchall()}
    if "is_active" not in prof_cols:
        conn.execute("ALTER TABLE profiles ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;")
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_profiles_chat_version
        ON profiles (chat_id, version);
        """
    )

    meme_cols = {r["name"] for r in conn.execute("PRAGMA table_info(memes);").fetchall()}
    if "profile_id" not in meme_cols:
        conn.execute("ALTER TABLE memes ADD COLUMN profile_id TEXT;")

    chats = conn.execute("SELECT DISTINCT chat_id FROM memes WHERE profile_id IS NULL;").fetchall()
    for c in chats:
        cid = c["chat_id"]
        latest_prof = conn.execute(
            "SELECT id FROM profiles WHERE chat_id = ? ORDER BY version DESC LIMIT 1;", (cid,)
        ).fetchone()
        if latest_prof:
            conn.execute(
                "UPDATE memes SET profile_id = ? WHERE chat_id = ? AND profile_id IS NULL;",
                (latest_prof["id"], cid),
            )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_memes_profile ON memes(profile_id);")


def normalize_active_profiles(conn: sqlite3.Connection) -> None:
    """Safely normalizes multiple-active profile flags.

    Ensures each chat has at most one active profile (latest version).
    """
    chats_with_multi_active = conn.execute(
        """
        SELECT chat_id FROM profiles
        WHERE is_active = 1
        GROUP BY chat_id
        HAVING count(*) > 1;
        """
    ).fetchall()

    for row in chats_with_multi_active:
        cid = row["chat_id"]
        latest = conn.execute(
            """
            SELECT id, version FROM profiles
            WHERE chat_id = ? AND is_active = 1
            ORDER BY version DESC LIMIT 1;
            """,
            (cid,),
        ).fetchone()
        if latest:
            conn.execute(
                """
                UPDATE profiles
                SET is_active = 0
                WHERE chat_id = ? AND is_active = 1 AND id != ?;
                """,
                (cid, latest["id"]),
            )


def migration_6_runtime_correctness_repairs(conn: sqlite3.Connection) -> None:
    """
    Additive migration (v6):
    1. analysis_chunks:
       - adds claimed_worker_id column for atomic worker claim on chunk execution.
       - adds payload_json column for durable bounded chunk payloads across failures and retries.
    2. Backfills payload_json from findings_json for chunks where findings_json holds
       a payload array.
    3. Normalizes multiple-active profiles safely without dropping user data:
       For any chat with multiple is_active = 1 profiles, retains the latest version active
       and marks older versions as inactive (is_active = 0).
    """
    chunk_cols = {r["name"] for r in conn.execute("PRAGMA table_info(analysis_chunks);").fetchall()}
    if "claimed_worker_id" not in chunk_cols:
        conn.execute("ALTER TABLE analysis_chunks ADD COLUMN claimed_worker_id TEXT;")
    if "payload_json" not in chunk_cols:
        conn.execute("ALTER TABLE analysis_chunks ADD COLUMN payload_json TEXT;")

    # Backfill payload_json where findings_json holds the initial chunk payload array
    conn.execute(
        """
        UPDATE analysis_chunks
        SET payload_json = findings_json
        WHERE payload_json IS NULL
          AND findings_json IS NOT NULL
          AND substr(trim(findings_json), 1, 1) = '[';
        """
    )
    # Clear findings_json if it was just the payload and status is not completed
    conn.execute(
        """
        UPDATE analysis_chunks
        SET findings_json = NULL
        WHERE status != 'completed'
          AND findings_json IS NOT NULL
          AND substr(trim(findings_json), 1, 1) = '[';
        """
    )

    normalize_active_profiles(conn)


def migration_7_configurable_chunk_plan(conn: sqlite3.Connection) -> None:
    """Additive migration (v7):

    analysis_jobs: adds chunk_size and context_overlap columns to persist plan identity.
    """
    job_cols = {r["name"] for r in conn.execute("PRAGMA table_info(analysis_jobs);").fetchall()}
    if "chunk_size" not in job_cols:
        conn.execute("ALTER TABLE analysis_jobs ADD COLUMN chunk_size INTEGER NOT NULL DEFAULT 50;")
    if "context_overlap" not in job_cols:
        conn.execute(
            "ALTER TABLE analysis_jobs ADD COLUMN context_overlap INTEGER NOT NULL DEFAULT 10;"
        )


class Database:
    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = str(db_path) if db_path is not None else str(settings.get_db_path())

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            isolation_level=None,  # Autocommit mode; we manage explicit transactions
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
        conn.create_function(
            "py_is_supported_text",
            2,
            lambda kind, text: 1 if is_supported_message(kind, text) else 0,
        )
        return conn

    @contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Generator[sqlite3.Connection, None, None]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def run_migrations(self) -> None:
        with self.connection() as conn:
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                """
            )
            applied = {
                row["version"]
                for row in conn.execute("SELECT version FROM schema_migrations;").fetchall()
            }

            for version, handler in MIGRATIONS:
                if version not in applied:
                    if callable(handler):
                        handler(conn)
                    else:
                        conn.executescript(handler)
                    now = datetime.now(UTC).isoformat()
                    conn.execute(
                        "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?);",
                        (version, now),
                    )

    def check_readiness(self) -> bool:
        try:
            with self.connection() as conn:
                row = conn.execute("SELECT 1;").fetchone()
                return bool(row and row[0] == 1)
        except (sqlite3.Error, OSError):
            return False


# Global default database instance
default_db = Database()


def get_db() -> Database:
    return default_db
