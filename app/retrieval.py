"""Chat-isolated SQLite FTS5 retrieval, safe literal search, and context expansion."""

import json
import re
from typing import Any

from app.db import Database
from app.models import ContextMessage, MemoryItem, PaginatedResponse

MAX_QUERY_LENGTH = 500


def escape_like(query: str, escape_char: str = "\\") -> str:
    """Escape LIKE wildcards (% and _) and the escape character itself."""
    s = query.replace(escape_char, escape_char + escape_char)
    s = s.replace("%", escape_char + "%")
    s = s.replace("_", escape_char + "_")
    return s


class MemoryRetriever:
    def __init__(self, db: Database) -> None:
        self.db = db

    def search_memories(
        self,
        chat_id: str,
        query: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> PaginatedResponse[MemoryItem]:
        # Enforce bounds on pagination
        safe_limit = max(1, min(limit, 100))
        safe_offset = max(0, offset)

        # Enforce bounded query length to avoid denial-of-service
        bounded_q = query[:MAX_QUERY_LENGTH]
        cleaned_q = bounded_q.strip()
        tokens = re.findall(r"[^\W_]+", cleaned_q, re.UNICODE)

        with self.db.connection() as conn:
            # First check if chat exists
            chat = conn.execute("SELECT id FROM chats WHERE id = ?;", (chat_id,)).fetchone()
            if not chat:
                raise ValueError("Chat not found")

            # Route 1: FTS5 token search if we have alphanumeric tokens
            if tokens:
                fts_match = " ".join(f'"{t}"' for t in tokens)
                # Count total matching
                count_sql = """
                    SELECT count(*)
                    FROM messages_fts f
                    JOIN messages m ON m.id = f.message_id
                    WHERE f.messages_fts MATCH ? AND f.chat_id = ? AND m.is_supported_text = 1;
                """
                total = conn.execute(count_sql, (fts_match, chat_id)).fetchone()[0]

                # Select paginated results ranked by bm25
                select_sql = """
                    SELECT m.id, m.chat_id, m.source_message_id, m.sender_id, m.sender_name,
                           m.sent_at, m.raw_date, m.kind, m.text, m.reply_to_source_id,
                           m.sticker_metadata_json, m.source_order,
                           bm25(messages_fts) as rank
                    FROM messages_fts f
                    JOIN messages m ON m.id = f.message_id
                    WHERE f.messages_fts MATCH ? AND f.chat_id = ? AND m.is_supported_text = 1
                    ORDER BY rank ASC, m.source_order ASC
                    LIMIT ? OFFSET ?;
                """
                rows = conn.execute(
                    select_sql, (fts_match, chat_id, safe_limit, safe_offset)
                ).fetchall()

            # Route 2: Non-token query with non-empty characters (e.g. emoji, percent, symbols)
            elif cleaned_q:
                escaped = escape_like(cleaned_q)
                like_pattern = f"%{escaped}%"
                count_sql = """
                    SELECT count(*)
                    FROM messages
                    WHERE chat_id = ? AND text LIKE ? ESCAPE '\\' AND is_supported_text = 1;
                """
                total = conn.execute(count_sql, (chat_id, like_pattern)).fetchone()[0]

                select_sql = """
                    SELECT id, chat_id, source_message_id, sender_id, sender_name,
                           sent_at, raw_date, kind, text, reply_to_source_id,
                           sticker_metadata_json, source_order
                    FROM messages
                    WHERE chat_id = ? AND text LIKE ? ESCAPE '\\' AND is_supported_text = 1
                    ORDER BY source_order ASC
                    LIMIT ? OFFSET ?;
                """
                rows = conn.execute(
                    select_sql, (chat_id, like_pattern, safe_limit, safe_offset)
                ).fetchall()

            # Route 3: Empty query (safe no-token branch: return all supported messages)
            else:
                count_sql = """
                    SELECT count(*)
                    FROM messages
                    WHERE chat_id = ? AND is_supported_text = 1;
                """
                total = conn.execute(count_sql, (chat_id,)).fetchone()[0]

                select_sql = """
                    SELECT id, chat_id, source_message_id, sender_id, sender_name,
                           sent_at, raw_date, kind, text, reply_to_source_id,
                           sticker_metadata_json, source_order
                    FROM messages
                    WHERE chat_id = ? AND is_supported_text = 1
                    ORDER BY source_order ASC
                    LIMIT ? OFFSET ?;
                """
                rows = conn.execute(select_sql, (chat_id, safe_limit, safe_offset)).fetchall()

            # Context enrichment for each item
            items: list[MemoryItem] = []
            for row in rows:
                item = self._hydrate_memory_item(conn, chat_id, row)
                items.append(item)

            return PaginatedResponse(
                items=items,
                total=total,
                limit=safe_limit,
                offset=safe_offset,
                query=query,
            )

    def get_candidate_quotes(
        self,
        chat_id: str,
        query: str,
        limit: int = 5,
        excluded_quote_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Retrieve candidate quotes for replies, incorporating feedback-aware ranking.
        Excludes specified quote IDs and down-ranks missed/overused quotes.
        """
        excluded_set = set(excluded_quote_ids or [])
        pool_limit = max(20, limit * 4)

        # Retrieve candidates pool using existing search logic
        pool_res = self.search_memories(chat_id, query=query, limit=pool_limit, offset=0)
        candidates = [
            item
            for item in pool_res.items
            if item.id not in excluded_set and str(item.source_message_id) not in excluded_set
        ]

        if not candidates:
            return []

        # Query feedback counts for candidate items
        candidate_ids = [c.id for c in candidates]
        feedback_by_quote: dict[str, dict[str, int]] = {}

        with self.db.connection() as conn:
            placeholders = ",".join("?" for _ in candidate_ids)
            sql = f"""
                SELECT r.quote_id, f.verdict, count(*) as cnt
                FROM feedback f
                JOIN replies r ON r.id = f.reply_id
                WHERE f.chat_id = ? AND r.quote_id IN ({placeholders})
                GROUP BY r.quote_id, f.verdict;
            """
            rows = conn.execute(sql, [chat_id, *candidate_ids]).fetchall()
            for r in rows:
                qid = r["quote_id"]
                verdict = r["verdict"]
                cnt = r["cnt"]
                if qid not in feedback_by_quote:
                    feedback_by_quote[qid] = {}
                feedback_by_quote[qid][verdict] = cnt

        # Score and rank candidates
        scored: list[tuple[float, MemoryItem]] = []
        for idx, item in enumerate(candidates):
            fb = feedback_by_quote.get(item.id, {})
            landed = fb.get("landed", 0)
            missed = fb.get("missed", 0)
            overused = fb.get("overused", 0)

            # Bounded deterministic ranking adjustment: [-5.0, 5.0]
            feedback_delta = min(5.0, max(-5.0, landed * 1.5 - missed * 1.5 - overused * 3.0))
            if overused >= 2:
                # Severe penalty for repeat overuse
                feedback_delta -= 10.0

            # Combined score: earlier items in pool had higher lexical relevance
            base_score = 10.0 - (idx * 0.2)
            total_score = base_score + feedback_delta
            scored.append((total_score, item))

        scored.sort(key=lambda x: x[0], reverse=True)

        result: list[dict[str, Any]] = []
        for _, item in scored[:limit]:
            result.append(
                {
                    "id": item.id,
                    "source_message_id": item.source_message_id,
                    "sender_name": item.sender_name,
                    "text": item.text,
                    "sent_at": item.sent_at,
                }
            )
        return result

    def _hydrate_memory_item(self, conn: Any, chat_id: str, row: Any) -> MemoryItem:
        sticker_meta = None
        if row["sticker_metadata_json"]:
            try:
                sticker_meta = json.loads(row["sticker_metadata_json"])
            except (json.JSONDecodeError, TypeError):
                sticker_meta = None

        # 1. Resolve reply parent within the same chat if reply_to_source_id is set
        reply_parent: ContextMessage | None = None
        reply_to_id = row["reply_to_source_id"]
        if reply_to_id is not None:
            parent_row = conn.execute(
                """
                SELECT id, source_message_id, sender_name, text, sent_at
                FROM messages
                WHERE chat_id = ? AND source_message_id = ?;
                """,
                (chat_id, reply_to_id),
            ).fetchone()
            if parent_row:
                reply_parent = ContextMessage(
                    id=parent_row["id"],
                    source_message_id=parent_row["source_message_id"],
                    sender_name=parent_row["sender_name"],
                    text=parent_row["text"],
                    sent_at=parent_row["sent_at"],
                    is_target=False,
                )

        # 2. Resolve nearby context (1 message before, target message, 1 message after)
        source_order = row["source_order"]
        prev_row = conn.execute(
            """
            SELECT id, source_message_id, sender_name, text, sent_at
            FROM messages
            WHERE chat_id = ? AND source_order < ?
            ORDER BY source_order DESC
            LIMIT 1;
            """,
            (chat_id, source_order),
        ).fetchone()

        next_row = conn.execute(
            """
            SELECT id, source_message_id, sender_name, text, sent_at
            FROM messages
            WHERE chat_id = ? AND source_order > ?
            ORDER BY source_order ASC
            LIMIT 1;
            """,
            (chat_id, source_order),
        ).fetchone()

        nearby: list[ContextMessage] = []
        if prev_row:
            nearby.append(
                ContextMessage(
                    id=prev_row["id"],
                    source_message_id=prev_row["source_message_id"],
                    sender_name=prev_row["sender_name"],
                    text=prev_row["text"],
                    sent_at=prev_row["sent_at"],
                    is_target=False,
                )
            )

        nearby.append(
            ContextMessage(
                id=row["id"],
                source_message_id=row["source_message_id"],
                sender_name=row["sender_name"],
                text=row["text"],
                sent_at=row["sent_at"],
                is_target=True,
            )
        )

        if next_row:
            nearby.append(
                ContextMessage(
                    id=next_row["id"],
                    source_message_id=next_row["source_message_id"],
                    sender_name=next_row["sender_name"],
                    text=next_row["text"],
                    sent_at=next_row["sent_at"],
                    is_target=False,
                )
            )

        return MemoryItem(
            id=row["id"],
            chat_id=row["chat_id"],
            source_message_id=row["source_message_id"],
            sender_id=row["sender_id"],
            sender_name=row["sender_name"],
            sent_at=row["sent_at"],
            raw_date=row["raw_date"] or "",
            kind=row["kind"],
            text=row["text"],
            reply_to_source_id=row["reply_to_source_id"],
            sticker_metadata=sticker_meta,
            reply_parent=reply_parent,
            nearby_context=nearby,
        )
