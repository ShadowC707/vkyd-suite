"""Ordered chunk analysis, durable ledger, hierarchical merge, and evidence validation."""

import asyncio
import contextlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from app.config import settings
from app.db import Database
from app.models import (
    AnalysisEstimateResponse,
    AnalysisJobResponse,
    MemeFinding,
    ProfileResponse,
)
from app.providers.base import (
    BaseAIProvider,
    ChunkAnalysisResult,
    ProviderError,
    StyleAnalysis,
)

DEFAULT_CHUNK_SIZE = 50
DEFAULT_CONTEXT_OVERLAP = 10
MAX_MESSAGE_CHARS = 4000
MAX_CHUNK_CHARS = 25000
MERGE_FAN_IN = 5
MAX_MERGE_NODE_PROMPT_CHARS = 18000



def compute_merge_calls(chunk_count: int, fan_in: int = MERGE_FAN_IN) -> int:
    """Calculate the exact number of intermediate and root merge calls required."""
    if chunk_count <= 0:
        return 0
    if chunk_count == 1:
        return 1
    calls = 0
    curr = chunk_count
    while curr > 1:
        if curr <= fan_in:
            calls += 1  # root merge
            break
        batches = (curr + fan_in - 1) // fan_in
        calls += batches
        curr = batches
    return calls


class AnalysisService:
    """Manages chat corpus chunking, durable progress ledger, and profile merge."""

    def __init__(
        self,
        db: Database,
        chunk_size: int | None = None,
        overlap: int | None = None,
        max_message_chars: int = MAX_MESSAGE_CHARS,
        max_chunk_chars: int = MAX_CHUNK_CHARS,
        merge_fan_in: int = MERGE_FAN_IN,
    ) -> None:
        self.db = db
        self.chunk_size = chunk_size if chunk_size is not None else settings.analysis_chunk_size
        self.overlap = overlap if overlap is not None else settings.analysis_context_overlap
        self.max_message_chars = max_message_chars
        self.max_chunk_chars = max_chunk_chars
        self.merge_fan_in = merge_fan_in

    def recover_stale_jobs(self) -> None:
        """On startup or service init, safely recover stranded running jobs to resumable state."""
        with self.db.transaction() as conn:
            conn.execute(
                """
                UPDATE analysis_chunks
                SET status = 'pending', claimed_worker_id = NULL
                WHERE status = 'running';
                """
            )
            now = datetime.now(UTC).isoformat()
            conn.execute(
                """
                UPDATE analysis_jobs
                SET status = 'paused_cancelled',
                    active_worker_id = NULL,
                    last_error_code = 'interrupted_restart',
                    updated_at = ?
                WHERE status = 'running';
                """,
                (now,),
            )
            from app.db import normalize_active_profiles

            normalize_active_profiles(conn)

    def _item_serialized_chars(self, item: dict[str, Any]) -> int:
        """Calculate complete serialized character length of message in prompt line format."""
        sender = (item.get("sender_name") or "Participant")[:100]
        src_id = item.get("source_message_id", 0)
        text = item.get("text") or ""
        return len(f"[ID: {src_id}] {sender}: {text}")

    def _plan_chunks(
        self, messages: list[dict[str, Any]]
    ) -> list[tuple[list[int], list[int], list[dict[str, Any]]]]:
        """
        Partition messages into bounded chunks.
        Splits oversized individual messages and enforces per-chunk character envelopes,
        accounting for sender names, metadata, and prompt formatting overhead.
        Returns: list of (source_ids_completed, context_ids, chunk_message_payloads).
        """
        # 1. Flatten messages with oversized text splitting
        flattened_items: list[dict[str, Any]] = []
        for m in messages:
            text = m["text"] or ""
            src_id = m["source_message_id"]
            sender = (m.get("sender_name") or "Participant")[:100]
            prefix_len = len(f"[Part 99/99] [ID: {src_id}] {sender}: ")
            max_slice_text_len = max(100, self.max_message_chars - prefix_len)

            if len(text) <= max_slice_text_len:
                flattened_items.append(
                    {
                        "source_message_id": src_id,
                        "sender_name": sender,
                        "sent_at": m["sent_at"],
                        "text": text,
                        "is_primary": True,
                    }
                )
            else:
                slices = [
                    text[i : i + max_slice_text_len]
                    for i in range(0, len(text), max_slice_text_len)
                ]
                total_parts = len(slices)
                for part_idx, slice_text in enumerate(slices):
                    flattened_items.append(
                        {
                            "source_message_id": src_id,
                            "sender_name": sender,
                            "sent_at": m["sent_at"],
                            "text": f"[Part {part_idx + 1}/{total_parts}] {slice_text}",
                            "is_primary": (part_idx == total_parts - 1),
                        }
                    )

        # 2. Group into chunks obeying chunk_size and max_chunk_chars
        chunks_plan: list[tuple[list[int], list[int], list[dict[str, Any]]]] = []
        total_items = len(flattened_items)
        idx = 0

        while idx < total_items:
            current_chunk_items: list[dict[str, Any]] = []
            chunk_chars = 0
            primary_source_ids: list[int] = []

            while idx < total_items and len(current_chunk_items) < self.chunk_size:
                item = flattened_items[idx]
                item_chars = self._item_serialized_chars(item)
                if current_chunk_items and (chunk_chars + item_chars > self.max_chunk_chars):
                    break
                current_chunk_items.append(item)
                chunk_chars += item_chars
                if item["is_primary"]:
                    primary_source_ids.append(item["source_message_id"])
                idx += 1

            # Determine context overlap from preceding items accounting for envelope bounds
            context_items: list[dict[str, Any]] = []
            start_of_chunk = idx - len(current_chunk_items)
            if start_of_chunk > 0:
                available_ctx_chars = max(0, self.max_chunk_chars - chunk_chars)
                curr_ctx_chars = 0
                temp_ctx: list[dict[str, Any]] = []
                ctx_limit = max(0, start_of_chunk - self.overlap)
                for back_idx in range(start_of_chunk - 1, ctx_limit - 1, -1):
                    ctx_item = flattened_items[back_idx]
                    i_chars = self._item_serialized_chars(ctx_item)
                    if curr_ctx_chars + i_chars > available_ctx_chars:
                        break
                    temp_ctx.append(ctx_item)
                    curr_ctx_chars += i_chars
                context_items = list(reversed(temp_ctx))

            context_ids = list(dict.fromkeys(ci["source_message_id"] for ci in context_items))
            full_payload = context_items + current_chunk_items
            chunks_plan.append((primary_source_ids, context_ids, full_payload))

        return chunks_plan

    def estimate_chunks(
        self,
        chat_id: str,
        chunk_size: int | None = None,
        overlap: int | None = None,
    ) -> AnalysisEstimateResponse:
        """Estimate chunk and call count including merge tree without making provider calls."""
        with self.db.connection() as conn:
            chat = conn.execute("SELECT id FROM chats WHERE id = ?;", (chat_id,)).fetchone()
            if not chat:
                raise ValueError(f"Chat '{chat_id}' not found.")

            rows = conn.execute(
                """
                SELECT source_message_id, sender_name, sent_at, text
                FROM messages
                WHERE chat_id = ? AND is_supported_text = 1
                ORDER BY source_order ASC;
                """,
                (chat_id,),
            ).fetchall()
            messages = [dict(r) for r in rows]

        plan_chunk_size = chunk_size if chunk_size is not None else self.chunk_size
        plan_overlap = overlap if overlap is not None else self.overlap

        orig_chunk_size = self.chunk_size
        orig_overlap = self.overlap
        try:
            self.chunk_size = plan_chunk_size
            self.overlap = plan_overlap
            chunks_plan = self._plan_chunks(messages)
        finally:
            self.chunk_size = orig_chunk_size
            self.overlap = orig_overlap

        total_supported = len(messages)
        estimated_chunks = len(chunks_plan)
        estimated_merge_calls = compute_merge_calls(estimated_chunks, self.merge_fan_in)
        estimated_total_calls = estimated_chunks + estimated_merge_calls

        return AnalysisEstimateResponse(
            chat_id=chat_id,
            total_supported_messages=total_supported,
            estimated_chunks=estimated_chunks,
            estimated_merge_calls=estimated_merge_calls,
            estimated_total_calls=estimated_total_calls,
            chunk_size=plan_chunk_size,
            context_overlap=plan_overlap,
            max_calls=settings.gemini_max_calls_per_analysis,
            provider_configured=settings.is_provider_configured,
        )

    def _is_merge_envelope_recoverable(self, conn: Any, job_row: Any) -> bool:
        """Return True if envelope error occurred during merge with valid base chunks."""
        if job_row["last_error_code"] not in ("envelope_exceeded", "merge_envelope_exceeded"):
            return True
        if job_row["completed_chunks"] < job_row["total_chunks"] or job_row["total_chunks"] <= 0:
            return False
        chat_row = conn.execute(
            "SELECT name FROM chats WHERE id = ?;", (job_row["chat_id"],)
        ).fetchone()
        chat_name = chat_row["name"] if chat_row else "Chat"
        chunk_rows = conn.execute(
            "SELECT findings_json FROM analysis_chunks WHERE job_id = ? AND status = 'completed';",
            (job_row["id"],),
        ).fetchall()
        if len(chunk_rows) != job_row["total_chunks"]:
            return False
        for cr in chunk_rows:
            if not cr["findings_json"]:
                return False
            try:
                item = ChunkAnalysisResult.model_validate_json(cr["findings_json"])
            except Exception:
                return False
            if self._calculate_merge_prompt_length([item], None, chat_name) > 38000:
                return False
        return True

    def get_job(self, chat_id: str, job_id: str) -> AnalysisJobResponse:
        """Fetch analysis job status and progress."""
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_jobs WHERE id = ? AND chat_id = ?;",
                (job_id, chat_id),
            ).fetchone()
            if not row:
                raise ValueError(f"Analysis job '{job_id}' not found for chat '{chat_id}'.")

            is_unrecoverable = row["last_error_code"] == "recovery_required" or (
                row["last_error_code"] in ("envelope_exceeded", "merge_envelope_exceeded")
                and not self._is_merge_envelope_recoverable(conn, row)
            )
            can_resume = row["status"] in (
                "paused_budget",
                "paused_cancelled",
                "failed",
            ) and not is_unrecoverable
            row_dict = dict(row)
            chunk_size_val = (
                row_dict["chunk_size"]
                if "chunk_size" in row_dict and row_dict["chunk_size"] is not None
                else self.chunk_size
            )
            overlap_val = (
                row_dict["context_overlap"]
                if "context_overlap" in row_dict and row_dict["context_overlap"] is not None
                else self.overlap
            )
            return AnalysisJobResponse(
                id=row["id"],
                chat_id=row["chat_id"],
                corpus_version=row["corpus_version"],
                status=row["status"],
                total_supported_messages=row["total_supported_messages"],
                processed_unique_messages=row["processed_unique_messages"],
                total_chunks=row["total_chunks"],
                completed_chunks=row["completed_chunks"],
                provider_calls=row["provider_calls"],
                max_calls=row["max_calls"],
                chunk_size=chunk_size_val,
                context_overlap=overlap_val,
                last_error_code=row["last_error_code"],
                can_resume=can_resume,
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )

    def find_active_or_resumable_job(self, chat_id: str) -> dict[str, Any] | None:
        """Find the latest active, paused, or failed job for this chat."""
        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM analysis_jobs
                WHERE chat_id = ?
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                (chat_id,),
            ).fetchone()
            return dict(row) if row else None

    def create_or_resume_job(
        self,
        chat_id: str,
        max_calls: int | None = None,
        chunk_size: int | None = None,
        overlap: int | None = None,
    ) -> AnalysisJobResponse:
        """
        Create a durable analysis plan and ledger for all supported text messages,
        or resume an existing incomplete job with explicit budget validation.
        """
        now = datetime.now(UTC).isoformat()

        with self.db.transaction() as conn:
            chat = conn.execute(
                "SELECT id, name, corpus_version FROM chats WHERE id = ?;",
                (chat_id,),
            ).fetchone()
            if not chat:
                raise ValueError(f"Chat '{chat_id}' not found.")

            corpus_version = chat["corpus_version"]

            existing_job = conn.execute(
                """
                SELECT * FROM analysis_jobs
                WHERE chat_id = ? AND corpus_version = ?
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                (chat_id, corpus_version),
            ).fetchone()

            if existing_job:
                is_unrecoverable = existing_job["last_error_code"] == "recovery_required" or (
                    existing_job["last_error_code"]
                    in ("envelope_exceeded", "merge_envelope_exceeded")
                    and not self._is_merge_envelope_recoverable(conn, existing_job)
                )
                if is_unrecoverable:
                    code = existing_job["last_error_code"]
                    raise ValueError(
                        f"Cannot resume job: unrecoverable error ({code}) requires "
                        "starting a new analysis job for this chat."
                    )
                status = existing_job["status"]
                target_job_id = existing_job["id"]
                current_calls = existing_job["provider_calls"]

                if status == "running":
                    # Already running; do not recreate or reset
                    pass
                elif status == "paused_budget":
                    # Resuming paused_budget REQUIRES explicit max_calls > current_calls.
                    # Omitted budget preserves the persisted cap, which is already reached.
                    if max_calls is None:
                        raise ValueError(
                            "Cannot resume paused_budget job: an explicit higher max_calls is "
                            f"required (current provider_calls={current_calls}, "
                            f"cap={existing_job['max_calls']})."
                        )
                    if max_calls <= current_calls:
                        raise ValueError(
                            f"Cannot resume paused_budget job: requested max_calls "
                            f"({max_calls}) must be greater than current provider_calls "
                            f"({current_calls})."
                        )
                    effective_max_calls = max_calls

                    conn.execute(
                        """
                        UPDATE analysis_jobs
                        SET status = 'pending',
                            max_calls = ?,
                            active_worker_id = NULL,
                            last_error_code = NULL,
                            updated_at = ?
                        WHERE id = ?;
                        """,
                        (effective_max_calls, now, target_job_id),
                    )
                    conn.execute(
                        """
                        UPDATE analysis_chunks
                        SET status = 'pending', claimed_worker_id = NULL
                        WHERE job_id = ? AND status IN ('failed', 'running');
                        """,
                        (target_job_id,),
                    )
                elif status in ("paused_cancelled", "failed"):
                    if max_calls is not None:
                        if max_calls < current_calls:
                            raise ValueError(
                                f"Cannot reduce max_calls ({max_calls}) below current "
                                f"provider_calls ({current_calls})."
                            )
                        effective_max_calls = max_calls
                    else:
                        # Omitted budget preserves the persisted cap; no silent increase
                        effective_max_calls = existing_job["max_calls"]

                    conn.execute(
                        """
                        UPDATE analysis_jobs
                        SET status = 'pending',
                            max_calls = ?,
                            active_worker_id = NULL,
                            last_error_code = NULL,
                            updated_at = ?
                        WHERE id = ?;
                        """,
                        (effective_max_calls, now, target_job_id),
                    )
                    conn.execute(
                        """
                        UPDATE analysis_chunks
                        SET status = 'pending', claimed_worker_id = NULL
                        WHERE job_id = ? AND status IN ('failed', 'running');
                        """,
                        (target_job_id,),
                    )
            else:
                effective_max_calls = (
                    max_calls if max_calls is not None else settings.gemini_max_calls_per_analysis
                )

                messages_rows = conn.execute(
                    """
                    SELECT id, source_message_id, sender_name, sent_at, text, source_order
                    FROM messages
                    WHERE chat_id = ? AND is_supported_text = 1
                    ORDER BY source_order ASC;
                    """,
                    (chat_id,),
                ).fetchall()
                messages = [dict(r) for r in messages_rows]
                total_supported = len(messages)

                job_id = str(uuid.uuid4())
                target_job_id = job_id

                plan_chunk_size = chunk_size if chunk_size is not None else self.chunk_size
                plan_overlap = overlap if overlap is not None else self.overlap

                orig_chunk_size = self.chunk_size
                orig_overlap = self.overlap
                try:
                    self.chunk_size = plan_chunk_size
                    self.overlap = plan_overlap
                    chunks_plan = self._plan_chunks(messages)
                finally:
                    self.chunk_size = orig_chunk_size
                    self.overlap = orig_overlap

                total_chunks = len(chunks_plan)

                conn.execute(
                    """
                    INSERT INTO analysis_jobs (
                        id, chat_id, corpus_version, status,
                        total_supported_messages, processed_unique_messages,
                        total_chunks, completed_chunks, provider_calls,
                        max_calls, chunk_size, context_overlap,
                        last_error_code, active_worker_id, created_at, updated_at
                    ) VALUES (?, ?, ?, 'pending', ?, 0, ?, 0, 0, ?, ?, ?, NULL, NULL, ?, ?);
                    """,
                    (
                        job_id,
                        chat_id,
                        corpus_version,
                        total_supported,
                        total_chunks,
                        effective_max_calls,
                        plan_chunk_size,
                        plan_overlap,
                        now,
                        now,
                    ),
                )

                for ordinal, (
                    src_ids,
                    ctx_ids,
                    chunk_payload,
                ) in enumerate(chunks_plan):
                    chunk_id = str(uuid.uuid4())
                    conn.execute(
                        """
                        INSERT INTO analysis_chunks (
                            id, job_id, ordinal, source_ids_json, context_ids_json,
                            status, findings_json, payload_json, attempts, claimed_worker_id
                        ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, 0, NULL);
                        """,
                        (
                            chunk_id,
                            job_id,
                            ordinal,
                            json.dumps(src_ids),
                            json.dumps(ctx_ids),
                            json.dumps(chunk_payload, ensure_ascii=False),
                        ),
                    )

        return self.get_job(chat_id, target_job_id)

    def _reserve_call(self, job_id: str, worker_id: str) -> bool:
        """
        Durably check and reserve one provider call attempt in the ledger.
        Returns True if the call is permitted and reserved; False if paused_budget or cancelled.
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            job = conn.execute(
                """
                SELECT status, active_worker_id, provider_calls, max_calls
                FROM analysis_jobs WHERE id = ?;
                """,
                (job_id,),
            ).fetchone()
            if not job:
                return False

            if job["status"] != "running" or job["active_worker_id"] != worker_id:
                return False

            if job["provider_calls"] >= job["max_calls"]:
                conn.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = 'paused_budget', updated_at = ?
                    WHERE id = ? AND active_worker_id = ?;
                    """,
                    (now, job_id, worker_id),
                )
                return False

            conn.execute(
                """
                UPDATE analysis_jobs
                SET provider_calls = provider_calls + 1, updated_at = ?
                WHERE id = ? AND active_worker_id = ?;
                """,
                (now, job_id, worker_id),
            )
            return True

    def _is_worker_active(self, job_id: str, worker_id: str) -> bool:
        """Check if this worker is still the active, non-cancelled owner of the job."""
        with self.db.connection() as conn:
            row = conn.execute(
                "SELECT status, active_worker_id FROM analysis_jobs WHERE id = ?;",
                (job_id,),
            ).fetchone()
            if not row:
                return False
            return row["status"] == "running" and row["active_worker_id"] == worker_id

    def _validate_chunk_payload(
        self, chunk_row: Any, chat_id: str | None = None
    ) -> list[dict[str, Any]] | None:
        """
        Validate whether a chunk's stored payload_json is present, well-formed,
        within character bounds, complete with respect to expected source and context IDs,
        consistent in split fragments, and belongs to the valid chat corpus.
        Returns parsed list of message dicts if valid, else None.
        """
        try:
            raw_payload = chunk_row["payload_json"]
        except (KeyError, IndexError):
            raw_payload = None

        if not raw_payload:
            # Also check if findings_json holds a valid payload array (legacy pre-v6 format)
            try:
                findings_raw = chunk_row["findings_json"]
                if (
                    findings_raw
                    and isinstance(findings_raw, str)
                    and findings_raw.strip().startswith("[")
                ):
                    raw_payload = findings_raw
            except (KeyError, IndexError):
                pass

        if not raw_payload:
            return None

        try:
            parsed = json.loads(raw_payload)
            if not isinstance(parsed, list) or not parsed:
                return None

            total_chars = 0
            payload_src_ids: set[int] = set()
            split_tracker: dict[int, list[tuple[int, int]]] = {}
            part_pattern = re.compile(r"^\[Part (\d+)/(\d+)\]")

            for item in parsed:
                if not isinstance(item, dict) or "source_message_id" not in item:
                    return None
                src_id = item["source_message_id"]
                if not isinstance(src_id, int):
                    return None
                payload_src_ids.add(src_id)
                total_chars += self._item_serialized_chars(item)

                item_text = item.get("text") or ""
                m = part_pattern.match(item_text)
                if m:
                    part_num, total_parts = int(m.group(1)), int(m.group(2))
                    if not (1 <= part_num <= total_parts):
                        return None
                    split_tracker.setdefault(src_id, []).append((part_num, total_parts))

            if total_chars > self.max_chunk_chars:
                return None

            # Validate expected source message completeness
            expected_src_ids: list[int] = []
            try:
                raw_src_ids = chunk_row["source_ids_json"]
                if raw_src_ids:
                    expected_src_ids = json.loads(raw_src_ids)
            except (KeyError, IndexError, json.JSONDecodeError):
                return None

            if not set(expected_src_ids).issubset(payload_src_ids):
                # Expected primary source messages are missing from the payload
                return None

            # Validate expected context message IDs if defined
            expected_ctx_ids: list[int] = []
            try:
                raw_ctx_ids = chunk_row["context_ids_json"]
                if raw_ctx_ids:
                    expected_ctx_ids = json.loads(raw_ctx_ids)
            except (KeyError, IndexError, json.JSONDecodeError):
                expected_ctx_ids = []

            # Ensure payload does not contain extraneous / foreign IDs
            allowed_ids = set(expected_src_ids) | set(expected_ctx_ids)
            for pid in payload_src_ids:
                if pid not in allowed_ids:
                    # Must be a leading split part whose completion is in a subsequent chunk
                    if pid not in split_tracker:
                        return None
                    parts = split_tracker[pid]
                    tot = parts[0][1]
                    if any(p == tot for p, _ in parts):
                        # Contains the final part but was not listed in expected_src_ids!
                        return None

            # Split-fragment completeness check:
            # If a source_id in expected_src_ids is split, this chunk must contain final Part Y/Y
            for exp_id in expected_src_ids:
                if exp_id in split_tracker:
                    parts = split_tracker[exp_id]
                    tot = parts[0][1]
                    has_final = any(p == tot for p, _ in parts)
                    if not has_final:
                        return None

            # Verify chat membership: all payload IDs must exist in chat's supported text messages
            target_chat_id = chat_id
            if not target_chat_id:
                try:
                    job_id = chunk_row["job_id"]
                    if job_id:
                        with self.db.connection() as conn:
                            j_row = conn.execute(
                                "SELECT chat_id FROM analysis_jobs WHERE id = ?;", (job_id,)
                            ).fetchone()
                            if j_row:
                                target_chat_id = j_row["chat_id"]
                except (KeyError, IndexError):
                    pass

            if target_chat_id and payload_src_ids:
                with self.db.connection() as conn:
                    placeholders = ",".join("?" for _ in payload_src_ids)
                    found_rows = conn.execute(
                        f"""
                        SELECT source_message_id FROM messages
                        WHERE chat_id = ? AND is_supported_text = 1
                          AND source_message_id IN ({placeholders});
                        """,
                        (target_chat_id, *payload_src_ids),
                    ).fetchall()
                    found_ids = {r["source_message_id"] for r in found_rows}
                    if found_ids != payload_src_ids:
                        return None

            return parsed
        except Exception:
            return None

    def _reconcile_legacy_or_corrupt_chunks(self, chat_id: str, job_id: str) -> None:
        """
        Durable reconciliation of legacy or corrupt chunk payloads.
        If all unfinished chunks have valid payload_json, leaves them untouched.
        If any unfinished chunk lacks valid payload_json, replans all unfinished messages
        into bounded chunks with exact unique coverage, preserving completed chunks.
        If exact reconstruction is impossible, marks job failed with 'recovery_required'.
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            chunks = conn.execute(
                """
                SELECT id, ordinal, status, findings_json, source_ids_json,
                       context_ids_json, payload_json
                FROM analysis_chunks
                WHERE job_id = ?
                ORDER BY ordinal ASC;
                """,
                (job_id,),
            ).fetchall()
            if not chunks:
                return

            completed_chunks = [c for c in chunks if c["status"] == "completed"]
            unfinished_chunks = [c for c in chunks if c["status"] != "completed"]

            if not unfinished_chunks:
                return

            # Check if all unfinished chunks are already validly planned with durable payloads
            all_valid = True
            for c in unfinished_chunks:
                validated_payload = self._validate_chunk_payload(c)
                if validated_payload is None:
                    all_valid = False
                    break
            if all_valid:
                return

            # An unfinished chunk has missing, malformed, or oversized payload. Reconcile!
            completed_src_ids: set[int] = set()
            for cc in completed_chunks:
                try:
                    s_ids = json.loads(cc["source_ids_json"])
                    completed_src_ids.update(s_ids)
                except Exception:
                    pass

            # Query all supported messages for this chat
            msg_rows = conn.execute(
                """
                SELECT id, source_message_id, sender_name, sent_at, text, source_order
                FROM messages
                WHERE chat_id = ? AND is_supported_text = 1
                ORDER BY source_order ASC;
                """,
                (chat_id,),
            ).fetchall()
            all_messages = [dict(r) for r in msg_rows]
            if not all_messages:
                conn.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = 'failed', last_error_code = 'recovery_required', updated_at = ?
                    WHERE id = ?;
                    """,
                    (now, job_id),
                )
                return

            # Filter messages to only those not yet completed by completed chunks
            unfinished_messages = [
                m for m in all_messages if m["source_message_id"] not in completed_src_ids
            ]

            if not unfinished_messages:
                # All messages completed, clean up obsolete unfinished chunks
                conn.execute(
                    "DELETE FROM analysis_chunks WHERE job_id = ? AND status != 'completed';",
                    (job_id,),
                )
                conn.execute(
                    """
                    UPDATE analysis_jobs
                    SET total_chunks = ?,
                        completed_chunks = ?,
                        processed_unique_messages = ?,
                        updated_at = ?
                    WHERE id = ?;
                    """,
                    (
                        len(completed_chunks),
                        len(completed_chunks),
                        len(completed_src_ids),
                        now,
                        job_id,
                    ),
                )
                return

            # Replan unfinished messages into bounded chunks
            new_chunks_plan = self._plan_chunks(unfinished_messages)
            if not new_chunks_plan:
                conn.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = 'failed', last_error_code = 'recovery_required', updated_at = ?
                    WHERE id = ?;
                    """,
                    (now, job_id),
                )
                return

            # Verify coverage invariance: new plan must cover exactly all unfinished message IDs
            planned_new_ids: set[int] = set()
            for src_ids, _, _ in new_chunks_plan:
                planned_new_ids.update(src_ids)

            expected_unfinished_ids = {m["source_message_id"] for m in unfinished_messages}
            if planned_new_ids != expected_unfinished_ids:
                # Reconstruction failed to maintain exact message coverage
                conn.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = 'failed', last_error_code = 'recovery_required', updated_at = ?
                    WHERE id = ?;
                    """,
                    (now, job_id),
                )
                return

            # Delete old unfinished chunks
            conn.execute(
                "DELETE FROM analysis_chunks WHERE job_id = ? AND status != 'completed';",
                (job_id,),
            )

            # Insert replacement bounded chunks with durable payload_json
            start_ordinal = (
                max(cc["ordinal"] for cc in completed_chunks) + 1 if completed_chunks else 0
            )
            for offset, (src_ids, ctx_ids, chunk_payload) in enumerate(new_chunks_plan):
                new_chunk_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO analysis_chunks (
                        id, job_id, ordinal, source_ids_json, context_ids_json,
                        status, findings_json, payload_json, attempts, claimed_worker_id
                    ) VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, 0, NULL);
                    """,
                    (
                        new_chunk_id,
                        job_id,
                        start_ordinal + offset,
                        json.dumps(src_ids),
                        json.dumps(ctx_ids),
                        json.dumps(chunk_payload, ensure_ascii=False),
                    ),
                )

            # Update job ledger
            total_chunks = len(completed_chunks) + len(new_chunks_plan)
            conn.execute(
                """
                UPDATE analysis_jobs
                SET total_chunks = ?,
                    completed_chunks = ?,
                    processed_unique_messages = ?,
                    total_supported_messages = ?,
                    updated_at = ?
                WHERE id = ?;
                """,
                (
                    total_chunks,
                    len(completed_chunks),
                    len(completed_src_ids),
                    len(all_messages),
                    now,
                    job_id,
                ),
            )

    def _calculate_merge_prompt_length(
        self,
        findings: list[ChunkAnalysisResult],
        current_profile: dict[str, Any] | None,
        chat_name: str,
    ) -> int:
        findings_summary = [f.model_dump() for f in findings]
        current_profile_summary = current_profile or {}
        prompt = (
            f"Synthesize the analysis findings for chat '{chat_name}'.\n\n"
            f"Current profile (if any):\n"
            f"{json.dumps(current_profile_summary, ensure_ascii=False)}\n\n"
            f"Chunk findings:\n"
            f"{json.dumps(findings_summary, ensure_ascii=False)}\n\n"
            "Produce a final MergeProfileResult with:\n"
            "1. instructions_md: clear markdown rules and personality guidelines "
            "(around 3,000-4,000 characters).\n"
            "2. structured: combined style attributes.\n"
            "3. memes: deduplicated list of the top 15-20 most authentic inside jokes "
            "with 2-5 preserved evidence IDs each."
        )
        return len(prompt)

    def _compact_oversized_intermediate(
        self,
        item: ChunkAnalysisResult,
        chat_name: str,
        target_prompt_chars: int = MAX_MERGE_NODE_PROMPT_CHARS,
    ) -> ChunkAnalysisResult:
        """Bound intermediate merge findings while preserving provenance and evidence IDs."""
        if self._calculate_merge_prompt_length([item], None, chat_name) <= target_prompt_chars:
            return item

        from app.providers.base import MemeCandidate

        # 1. Deduplicate memes by normalized label, uniting evidence IDs
        deduped: dict[str, MemeCandidate] = {}
        for m in item.memes:
            key = m.label.strip().lower()
            if key in deduped:
                existing = deduped[key]
                combined_ids = list(
                    dict.fromkeys(
                        existing.evidence_source_message_ids + m.evidence_source_message_ids
                    )
                )[:10]
                deduped[key] = MemeCandidate(
                    label=existing.label,
                    description=existing.description,
                    usage_context=existing.usage_context,
                    evidence_source_message_ids=combined_ids,
                    confidence=max(existing.confidence, m.confidence),
                )
            else:
                deduped[key] = m

        sorted_memes = sorted(
            deduped.values(),
            key=lambda x: (len(x.evidence_source_message_ids) > 0, x.confidence),
            reverse=True,
        )

        # Progressive compaction tiers until prompt fits target_prompt_chars
        for max_memes, max_desc, max_summary, max_ev in [
            (25, 450, 4500, 8),
            (20, 350, 4000, 5),
            (15, 250, 3000, 5),
            (10, 200, 2000, 4),
        ]:
            compacted_memes = [
                MemeCandidate(
                    label=m.label[:150],
                    description=m.description[:max_desc],
                    usage_context=(m.usage_context or "")[:max_desc],
                    evidence_source_message_ids=m.evidence_source_message_ids[:max_ev],
                    confidence=m.confidence,
                )
                for m in sorted_memes[:max_memes]
            ]
            candidate = ChunkAnalysisResult(
                style=item.style,
                memes=compacted_memes,
                instructions_summary=item.instructions_summary[:max_summary],
            )
            cand_len = self._calculate_merge_prompt_length([candidate], None, chat_name)
            if cand_len <= target_prompt_chars:
                return candidate

        return candidate

    def _partition_merge_batches(
        self,
        items: list[ChunkAnalysisResult],
        chat_name: str,
        max_fan_in: int = MERGE_FAN_IN,
        max_prompt_chars: int = 38000,
    ) -> list[list[ChunkAnalysisResult]]:
        """
        Deterministically partition findings into bounded batches that fit the serialized envelope.
        Ensures every batch's prompt is <= max_prompt_chars and <= max_fan_in items.
        Preserves findings without mutation.
        If an individual node exceeds the envelope limit, raises ProviderError("envelope_exceeded").
        """
        batches: list[list[ChunkAnalysisResult]] = []
        curr_batch: list[ChunkAnalysisResult] = []

        for item in items:
            node_prompt_len = self._calculate_merge_prompt_length([item], None, chat_name)
            if node_prompt_len > max_prompt_chars:
                msg = f"Individual node exceeds envelope ({node_prompt_len} > {max_prompt_chars})."
                raise ProviderError("envelope_exceeded", msg)

            if not curr_batch:
                curr_batch.append(item)
                continue

            candidate = curr_batch + [item]
            if (
                len(candidate) <= max_fan_in
                and self._calculate_merge_prompt_length(candidate, None, chat_name)
                <= max_prompt_chars
            ):
                curr_batch.append(item)
            else:
                batches.append(curr_batch)
                curr_batch = [item]

        if curr_batch:
            batches.append(curr_batch)

        if len(batches) == len(items) and len(items) > 1:
            compact_threshold = min(MAX_MERGE_NODE_PROMPT_CHARS, max_prompt_chars // 2)
            has_compactable_node = any(
                self._calculate_merge_prompt_length([it], None, chat_name) > compact_threshold
                for it in items
            )
            if not has_compactable_node or max_fan_in < 2:
                raise ProviderError(
                    "envelope_exceeded",
                    "Individual merge node findings are too large to fit the merge envelope.",
                )

        return batches

    async def execute_job(
        self,
        chat_id: str,
        job_id: str,
        provider: BaseAIProvider,
        concurrency: int | None = None,
    ) -> AnalysisJobResponse:
        """
        Atomically claim and execute analysis job with bounded envelopes, sticky cancellation,
        pre-call budget reservation, bounded async worker pool, and recursive merge.
        """
        worker_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        pool_size = max(
            1,
            concurrency if concurrency is not None else settings.gemini_analysis_concurrency,
        )

        # 1. Atomically claim job
        with self.db.transaction() as conn:
            chat = conn.execute("SELECT id, name FROM chats WHERE id = ?;", (chat_id,)).fetchone()
            if not chat:
                raise ValueError(f"Chat '{chat_id}' not found.")
            chat_name = chat["name"]

            # Claim if pending or if already running by this worker
            cursor = conn.execute(
                """
                UPDATE analysis_jobs
                SET status = 'running',
                    active_worker_id = ?,
                    last_error_code = NULL,
                    updated_at = ?
                WHERE id = ? AND (
                    status = 'pending'
                    OR (status = 'running' AND (active_worker_id IS NULL OR active_worker_id = ?))
                );
                """,
                (worker_id, now, job_id, worker_id),
            )
            if cursor.rowcount == 0:
                # Job is already claimed by another active worker or is not in runnable state
                return self.get_job(chat_id, job_id)

        # 1b. Reconcile legacy or corrupt chunks durably before dispatch
        self._reconcile_legacy_or_corrupt_chunks(chat_id, job_id)
        with self.db.connection() as conn:
            j = conn.execute(
                "SELECT status, last_error_code FROM analysis_jobs WHERE id = ?;", (job_id,)
            ).fetchone()
            if j and j["status"] == "failed" and j["last_error_code"] == "recovery_required":
                raise ProviderError(
                    "recovery_required",
                    "Analysis job requires safe recovery and cannot be dispatched.",
                )

        try:
            # 2. Bounded Async Worker Pool for Chunk Processing
            stop_event = asyncio.Event()
            first_error: Exception | None = None
            error_lock = asyncio.Lock()
            db_lock = asyncio.Lock()

            async def worker_loop(worker_idx: int) -> None:
                nonlocal first_error
                while not stop_event.is_set():
                    if not self._is_worker_active(job_id, worker_id):
                        stop_event.set()
                        break

                    # Select & atomically claim next pending chunk
                    async with db_lock:
                        with self.db.transaction() as conn:
                            job = conn.execute(
                                "SELECT status, active_worker_id FROM analysis_jobs WHERE id = ?;",
                                (job_id,),
                            ).fetchone()
                            if (
                                not job
                                or job["status"] != "running"
                                or job["active_worker_id"] != worker_id
                            ):
                                stop_event.set()
                                break

                            next_chunk = conn.execute(
                                """
                                SELECT * FROM analysis_chunks
                                WHERE job_id = ? AND status = 'pending'
                                ORDER BY ordinal ASC
                                LIMIT 1;
                                """,
                                (job_id,),
                            ).fetchone()

                            if not next_chunk:
                                # No more pending chunks to process
                                break

                            chunk_id = next_chunk["id"]
                            claim_cur = conn.execute(
                                """
                                UPDATE analysis_chunks
                                SET status = 'running', claimed_worker_id = ?,
                                    attempts = attempts + 1
                                WHERE id = ? AND status = 'pending';
                                """,
                                (worker_id, chunk_id),
                            )
                            if claim_cur.rowcount == 0:
                                continue

                    src_ids = json.loads(next_chunk["source_ids_json"])
                    chunk_messages = self._validate_chunk_payload(next_chunk, chat_id=chat_id)
                    if chunk_messages is None:
                        async with error_lock:
                            if first_error is None:
                                first_error = ProviderError(
                                    "recovery_required",
                                    "Chunk payload is missing or malformed; "
                                    "safe recovery required.",
                                )
                        stop_event.set()
                        async with db_lock:
                            with self.db.transaction() as conn:
                                conn.execute(
                                    """
                                    UPDATE analysis_chunks
                                    SET status = 'failed', claimed_worker_id = NULL
                                    WHERE id = ? AND status = 'running' AND claimed_worker_id = ?;
                                    """,
                                    (chunk_id, worker_id),
                                )
                        break

                    # Pre-reserve call attempt
                    async with db_lock:
                        reserved = self._reserve_call(job_id, worker_id)
                        if not reserved:
                            with self.db.transaction() as conn:
                                conn.execute(
                                    """
                                    UPDATE analysis_chunks
                                    SET status = 'pending', claimed_worker_id = NULL
                                    WHERE id = ? AND status = 'running' AND claimed_worker_id = ?;
                                    """,
                                    (chunk_id, worker_id),
                                )
                            stop_event.set()
                            break

                    # Call provider (concurrent across pool workers)
                    try:
                        findings = await provider.analyze_chunk(chunk_messages, chat_name)
                    except Exception as call_err:
                        async with error_lock:
                            if first_error is None:
                                first_error = call_err
                        stop_event.set()
                        async with db_lock:
                            with self.db.transaction() as tx:
                                tx.execute(
                                    """
                                    UPDATE analysis_chunks
                                    SET status = 'failed', claimed_worker_id = NULL
                                    WHERE id = ? AND status = 'running' AND claimed_worker_id = ?;
                                    """,
                                    (chunk_id, worker_id),
                                )
                        break

                    # Check cancellation & commit chunk atomically
                    now_str = datetime.now(UTC).isoformat()
                    async with db_lock:
                        with self.db.transaction() as conn:
                            job = conn.execute(
                                "SELECT status, active_worker_id FROM analysis_jobs WHERE id = ?;",
                                (job_id,),
                            ).fetchone()
                            if (
                                not job
                                or job["active_worker_id"] != worker_id
                                or job["status"] in ("paused_cancelled", "failed")
                            ):
                                conn.execute(
                                    """
                                    UPDATE analysis_chunks
                                    SET status = 'pending', claimed_worker_id = NULL
                                    WHERE id = ? AND status = 'running' AND claimed_worker_id = ?;
                                    """,
                                    (chunk_id, worker_id),
                                )
                                stop_event.set()
                                break

                            c_cur = conn.execute(
                                """
                                UPDATE analysis_chunks
                                SET status = 'completed', findings_json = ?,
                                    claimed_worker_id = NULL
                                WHERE id = ? AND status = 'running' AND claimed_worker_id = ?;
                                """,
                                (findings.model_dump_json(), chunk_id, worker_id),
                            )
                            if c_cur.rowcount > 0:
                                conn.execute(
                                    """
                                    UPDATE analysis_jobs
                                    SET completed_chunks = completed_chunks + 1,
                                        processed_unique_messages = processed_unique_messages + ?,
                                        updated_at = ?
                                    WHERE id = ? AND status IN ('running', 'paused_budget')
                                      AND active_worker_id = ?;
                                    """,
                                    (len(src_ids), now_str, job_id, worker_id),
                                )

            # Run workers concurrently and await their completion
            worker_tasks = [asyncio.create_task(worker_loop(i)) for i in range(pool_size)]
            await asyncio.gather(*worker_tasks)

            # If an error occurred in any worker, mark job failed and raise
            if first_error is not None:
                err_code = getattr(first_error, "code", "provider_error")
                err_now = datetime.now(UTC).isoformat()
                with self.db.transaction() as conn:
                    conn.execute(
                        """
                        UPDATE analysis_jobs
                        SET status = 'failed', last_error_code = ?, updated_at = ?
                        WHERE id = ? AND status = 'running' AND active_worker_id = ?;
                        """,
                        (err_code, err_now, job_id, worker_id),
                    )
                raise first_error

            # If budget was reached, job is paused_budget; return early
            with self.db.connection() as conn:
                j_check = conn.execute(
                    "SELECT status, active_worker_id FROM analysis_jobs WHERE id = ?;",
                    (job_id,),
                ).fetchone()
                if (
                    not j_check
                    or j_check["status"] != "running"
                    or j_check["active_worker_id"] != worker_id
                ):
                    return self.get_job(chat_id, job_id)

                unfinished = conn.execute(
                    """
                    SELECT count(*) FROM analysis_chunks
                    WHERE job_id = ? AND status != 'completed';
                    """,
                    (job_id,),
                ).fetchone()[0]
                if unfinished > 0:
                    return self.get_job(chat_id, job_id)

            # Gating: all chunks must be validly completed with exact unique message coverage
            with self.db.connection() as conn:
                job_row = conn.execute(
                    "SELECT * FROM analysis_jobs WHERE id = ?;", (job_id,)
                ).fetchone()
                if (
                    not job_row
                    or job_row["status"] != "running"
                    or job_row["active_worker_id"] != worker_id
                ):
                    return self.get_job(chat_id, job_id)

                chunk_rows = conn.execute(
                    """
                    SELECT ordinal, status, findings_json, source_ids_json
                    FROM analysis_chunks
                    WHERE job_id = ?
                    ORDER BY ordinal ASC;
                    """,
                    (job_id,),
                ).fetchall()

            if len(chunk_rows) != job_row["total_chunks"]:
                return self.get_job(chat_id, job_id)

            completed_src_ids: set[int] = set()
            validated_chunk_findings: list[ChunkAnalysisResult] = []
            for c in chunk_rows:
                if c["status"] != "completed":
                    # An unfinished chunk exists; do not merge or complete
                    return self.get_job(chat_id, job_id)
                if not c["findings_json"]:
                    raise ProviderError(
                        "internal_error",
                        "Completed chunk findings are missing.",
                    )
                try:
                    res = ChunkAnalysisResult.model_validate_json(c["findings_json"])
                    validated_chunk_findings.append(res)
                except Exception as val_err:
                    raise ProviderError(
                        "internal_error",
                        "Completed chunk findings failed schema validation.",
                    ) from val_err
                src_ids = json.loads(c["source_ids_json"])
                completed_src_ids.update(src_ids)

            if len(completed_src_ids) != job_row["total_supported_messages"]:
                return self.get_job(chat_id, job_id)

            # 3. Hierarchical recursive merge
            await self._merge_and_save_profile(
                chat_id,
                job_id,
                chat_name,
                provider,
                worker_id,
                validated_chunk_findings,
                concurrency=pool_size,
            )

        except ProviderError as pe:
            now = datetime.now(UTC).isoformat()
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = 'failed', last_error_code = ?, updated_at = ?
                    WHERE id = ? AND status = 'running' AND active_worker_id = ?;
                    """,
                    (pe.code, now, job_id, worker_id),
                )
            raise
        except Exception as e:
            now = datetime.now(UTC).isoformat()
            with self.db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = 'failed', last_error_code = 'internal_error', updated_at = ?
                    WHERE id = ? AND status = 'running' AND active_worker_id = ?;
                    """,
                    (now, job_id, worker_id),
                )
            raise ProviderError(
                "internal_error",
                "Analysis execution encountered an unexpected internal error.",
            ) from e

        return self.get_job(chat_id, job_id)

    async def _execute_merge_level(
        self,
        job_id: str,
        worker_id: str,
        chat_name: str,
        next_level: int,
        batches: list[list[ChunkAnalysisResult]],
        provider: BaseAIProvider,
        pool_size: int,
        total_input_items: int = 0,
    ) -> list[ChunkAnalysisResult] | None:
        """Execute a single level of intermediate merge batches concurrently."""
        next_level_items: list[ChunkAnalysisResult | None] = [None] * len(batches)
        merge_queue: asyncio.Queue[tuple[int, list[ChunkAnalysisResult]]] = asyncio.Queue()
        for idx, batch in enumerate(batches):
            merge_queue.put_nowait((idx, batch))

        stop_merge_event = asyncio.Event()
        first_merge_err: Exception | None = None
        merge_err_lock = asyncio.Lock()
        merge_db_lock = asyncio.Lock()
        recomputed_any = False

        async def worker() -> None:
            nonlocal first_merge_err, recomputed_any
            while not stop_merge_event.is_set():
                try:
                    node_idx, batch = merge_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                try:
                    if not self._is_worker_active(job_id, worker_id):
                        stop_merge_event.set()
                        break

                    single_item = len(batch) == 1
                    single_item_prompt_len = (
                        self._calculate_merge_prompt_length(batch, None, chat_name)
                        if single_item
                        else 0
                    )
                    needs_single_compaction = single_item and (
                        single_item_prompt_len > MAX_MERGE_NODE_PROMPT_CHARS
                        or (total_input_items > 1 and len(batches) == total_input_items)
                    )

                    # If batch has only 1 item and does not need compaction,
                    # it passes through to next level directly
                    if single_item and not needs_single_compaction:
                        next_level_items[node_idx] = batch[0]
                        continue

                    # If batch has 1 item and needs compaction:
                    if single_item and needs_single_compaction:
                        # Check if already compacted in merge nodes
                        async with merge_db_lock:
                            with self.db.connection() as conn:
                                existing_node = conn.execute(
                                    """
                                    SELECT findings_json FROM analysis_merge_nodes
                                    WHERE job_id = ? AND level = ? AND node_index = ?
                                      AND status = 'completed';
                                    """,
                                    (job_id, next_level, node_idx),
                                ).fetchone()

                        if existing_node and existing_node["findings_json"]:
                            try:
                                loaded = ChunkAnalysisResult.model_validate_json(
                                    existing_node["findings_json"]
                                )
                                loaded_len = self._calculate_merge_prompt_length(
                                    [loaded], None, chat_name
                                )
                                if loaded_len <= MAX_MERGE_NODE_PROMPT_CHARS:
                                    next_level_items[node_idx] = loaded
                                    continue
                            except Exception:
                                pass

                        compacted = self._compact_oversized_intermediate(batch[0], chat_name)
                        node_id = str(uuid.uuid4())
                        now_str = datetime.now(UTC).isoformat()
                        async with merge_db_lock:
                            recomputed_any = True
                            with self.db.transaction() as conn:
                                conn.execute(
                                    """
                                    INSERT OR REPLACE INTO analysis_merge_nodes (
                                        id, job_id, level, node_index, status,
                                        findings_json, created_at
                                    ) VALUES (?, ?, ?, ?, 'completed', ?, ?);
                                    """,
                                    (
                                        node_id,
                                        job_id,
                                        next_level,
                                        node_idx,
                                        compacted.model_dump_json(),
                                        now_str,
                                    ),
                                )
                        next_level_items[node_idx] = compacted
                        continue

                    # Check if this merge node is already completed and persisted
                    async with merge_db_lock:
                        with self.db.connection() as conn:
                            existing_node = conn.execute(
                                """
                                SELECT findings_json FROM analysis_merge_nodes
                                WHERE job_id = ? AND level = ? AND node_index = ?
                                  AND status = 'completed';
                                """,
                                (job_id, next_level, node_idx),
                            ).fetchone()

                    if existing_node and existing_node["findings_json"]:
                        try:
                            loaded = ChunkAnalysisResult.model_validate_json(
                                existing_node["findings_json"]
                            )
                            loaded_len = self._calculate_merge_prompt_length(
                                [loaded], None, chat_name
                            )
                            if loaded_len <= MAX_MERGE_NODE_PROMPT_CHARS:
                                next_level_items[node_idx] = loaded
                                continue
                        except Exception as e:
                            raise ProviderError(
                                "internal_error",
                                "Intermediate merge node findings failed schema validation.",
                            ) from e

                    # Local preflight validation before reserving call: must not consume budget
                    batch_prompt_len = self._calculate_merge_prompt_length(batch, None, chat_name)
                    if batch_prompt_len > 40000:
                        raise ProviderError(
                            "envelope_exceeded",
                            f"Intermediate prompt exceeds envelope ({batch_prompt_len} > 40k).",
                        )

                    # Reserve call attempt
                    async with merge_db_lock:
                        reserved = self._reserve_call(job_id, worker_id)
                    if not reserved:
                        stop_merge_event.set()
                        break

                    # Provider merge call
                    batch_res = await provider.merge_findings(batch, None, chat_name)

                    if not self._is_worker_active(job_id, worker_id):
                        stop_merge_event.set()
                        break

                    intermediate = ChunkAnalysisResult(
                        style=batch_res.structured,
                        memes=batch_res.memes,
                        instructions_summary=batch_res.instructions_md,
                    )
                    intermediate = self._compact_oversized_intermediate(intermediate, chat_name)

                    # Persist intermediate merge node
                    node_id = str(uuid.uuid4())
                    now_str = datetime.now(UTC).isoformat()
                    async with merge_db_lock:
                        recomputed_any = True
                        with self.db.transaction() as conn:
                            conn.execute(
                                """
                                INSERT OR REPLACE INTO analysis_merge_nodes (
                                    id, job_id, level, node_index, status,
                                    findings_json, created_at
                                ) VALUES (?, ?, ?, ?, 'completed', ?, ?);
                                """,
                                (
                                    node_id,
                                    job_id,
                                    next_level,
                                    node_idx,
                                    intermediate.model_dump_json(),
                                    now_str,
                                ),
                            )

                    next_level_items[node_idx] = intermediate
                except Exception as m_err:
                    async with merge_err_lock:
                        if first_merge_err is None:
                            first_merge_err = m_err
                    stop_merge_event.set()
                    break
                finally:
                    with contextlib.suppress(ValueError):
                        merge_queue.task_done()

        actual_pool_size = min(len(batches), pool_size)
        tasks = [asyncio.create_task(worker()) for _ in range(actual_pool_size)]
        await asyncio.gather(*tasks)

        if first_merge_err is not None:
            raise first_merge_err

        if not self._is_worker_active(job_id, worker_id):
            return None

        if any(item is None for item in next_level_items):
            return None

        with self.db.transaction() as conn:
            if recomputed_any:
                conn.execute(
                    """
                    DELETE FROM analysis_merge_nodes
                    WHERE job_id = ? AND level > ?;
                    """,
                    (job_id, next_level),
                )
            conn.execute(
                """
                DELETE FROM analysis_merge_nodes
                WHERE job_id = ? AND level = ? AND node_index >= ?;
                """,
                (job_id, next_level, len(batches)),
            )

        return [item for item in next_level_items if item is not None]

    async def _merge_and_save_profile(
        self,
        chat_id: str,
        job_id: str,
        chat_name: str,
        provider: BaseAIProvider,
        worker_id: str,
        chunk_findings: list[ChunkAnalysisResult] | None = None,
        concurrency: int | None = None,
    ) -> None:
        """
        Hierarchically merge chunk findings in bounded rounds, persisting intermediate
        merge nodes for resumability, validating evidence IDs, and preserving owner profiles.
        """
        if not self._is_worker_active(job_id, worker_id):
            return

        pool_size = max(
            1,
            concurrency if concurrency is not None else settings.gemini_analysis_concurrency,
        )

        if chunk_findings is None:
            with self.db.connection() as conn:
                chunks = conn.execute(
                    """
                    SELECT findings_json FROM analysis_chunks
                    WHERE job_id = ? AND status = 'completed'
                    ORDER BY ordinal ASC;
                    """,
                    (job_id,),
                ).fetchall()

            chunk_findings = []
            for c in chunks:
                raw = c["findings_json"]
                if raw:
                    try:
                        chunk_findings.append(ChunkAnalysisResult.model_validate_json(raw))
                    except Exception as e:
                        raise ProviderError(
                            "internal_error",
                            "Completed chunk findings failed schema validation.",
                        ) from e

        with self.db.connection() as conn:
            # Active profile for synthesis context
            active_profile_row = conn.execute(
                """
                SELECT * FROM profiles WHERE chat_id = ? AND is_active = 1
                ORDER BY version DESC LIMIT 1;
                """,
                (chat_id,),
            ).fetchone()

        if not chunk_findings:
            chunk_findings.append(
                ChunkAnalysisResult(
                    style=StyleAnalysis(),
                    memes=[],
                    instructions_summary="No messages to analyze.",
                )
            )

        current_profile_dict = (
            {
                "version": active_profile_row["version"],
                "instructions_md": active_profile_row["instructions_md"],
                "structured": json.loads(active_profile_row["structured_json"]),
            }
            if active_profile_row
            else None
        )

        # Recursive bounded tree reduction adapting batches to serialized prompt envelope
        curr_items = chunk_findings
        curr_level = 0

        while len(curr_items) > 1 and (
            len(curr_items) > self.merge_fan_in
            or self._calculate_merge_prompt_length(curr_items, current_profile_dict, chat_name)
            > 38000
        ):
            next_level = curr_level + 1
            batches = self._partition_merge_batches(curr_items, chat_name, self.merge_fan_in, 38000)
            level_results = await self._execute_merge_level(
                job_id=job_id,
                worker_id=worker_id,
                chat_name=chat_name,
                next_level=next_level,
                batches=batches,
                provider=provider,
                pool_size=pool_size,
                total_input_items=len(curr_items),
            )
            if level_results is None:
                return

            curr_items = level_results
            curr_level = next_level

        # Final root merge
        if not self._is_worker_active(job_id, worker_id):
            return

        # Preflight envelope validation before reserving root call attempt
        root_prompt_len = self._calculate_merge_prompt_length(
            curr_items, current_profile_dict, chat_name
        )
        if root_prompt_len > 40000:
            raise ProviderError(
                "envelope_exceeded",
                f"Merge prompt exceeds maximum envelope ({root_prompt_len} > 40000 chars).",
            )

        if not self._reserve_call(job_id, worker_id):
            return

        merged = await provider.merge_findings(curr_items, current_profile_dict, chat_name)

        if not self._is_worker_active(job_id, worker_id):
            return

        # Evidence validation: filter meme evidence message IDs against active chat
        with self.db.connection() as conn:
            valid_source_ids = {
                r[0]
                for r in conn.execute(
                    "SELECT source_message_id FROM messages WHERE chat_id = ?;",
                    (chat_id,),
                ).fetchall()
            }

        valid_memes: list[MemeFinding] = []
        for m in merged.memes:
            sanitized_evidence = [
                eid for eid in m.evidence_source_message_ids if eid in valid_source_ids
            ]
            valid_memes.append(
                MemeFinding(
                    id=str(uuid.uuid4()),
                    chat_id=chat_id,
                    label=(m.label or "Локальний мем").strip()[:150] or "Локальний мем",
                    description=(m.description or m.label or "Жарт чату").strip()[:500]
                    or "Жарт чату",
                    usage_context=m.usage_context[:500] if m.usage_context else None,
                    evidence_message_ids=sanitized_evidence[:50],
                    confidence=max(0.0, min(1.0, float(m.confidence))),
                    approval_state="approved" if sanitized_evidence else "unverified",
                )
            )

        # Atomic profile and meme persistence preserving owner active profile
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            # Re-verify worker ownership and job status
            job = conn.execute(
                "SELECT status, active_worker_id FROM analysis_jobs WHERE id = ?;",
                (job_id,),
            ).fetchone()
            if not job or job["status"] != "running" or job["active_worker_id"] != worker_id:
                return

            # Check if active profile was edited by owner
            latest_active = conn.execute(
                """
                SELECT * FROM profiles
                WHERE chat_id = ? AND is_active = 1
                ORDER BY version DESC LIMIT 1;
                """,
                (chat_id,),
            ).fetchone()

            if latest_active and latest_active["edited_by_user"]:
                # Preserves owner profile: analysis results become a suggestion
                new_is_active = 0
            else:
                new_is_active = 1
                # Deactivate older profiles
                conn.execute(
                    "UPDATE profiles SET is_active = 0 WHERE chat_id = ?;",
                    (chat_id,),
                )

            # Transactional unique version allocation
            max_ver_row = conn.execute(
                "SELECT coalesce(max(version), 0) FROM profiles WHERE chat_id = ?;",
                (chat_id,),
            ).fetchone()
            next_version = (max_ver_row[0] if max_ver_row else 0) + 1

            struct_payload = (
                merged.structured.model_dump()
                if hasattr(merged.structured, "model_dump")
                else merged.structured
            )

            profile_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO profiles (
                    id, chat_id, version, source_job_id,
                    instructions_md, structured_json, edited_by_user, is_active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?);
                """,
                (
                    profile_id,
                    chat_id,
                    next_version,
                    job_id,
                    merged.instructions_md,
                    json.dumps(struct_payload, ensure_ascii=False),
                    new_is_active,
                    now,
                ),
            )

            # Insert memes linked to this profile
            for vm in valid_memes:
                conn.execute(
                    """
                    INSERT INTO memes (
                        id, chat_id, profile_id, label, description, usage_context,
                        evidence_message_ids, confidence, approval_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        vm.id,
                        vm.chat_id,
                        profile_id,
                        vm.label,
                        vm.description,
                        vm.usage_context,
                        json.dumps(vm.evidence_message_ids),
                        vm.confidence,
                        vm.approval_state,
                    ),
                )

            # Mark job completed
            conn.execute(
                """
                UPDATE analysis_jobs
                SET status = 'completed',
                    active_worker_id = NULL,
                    updated_at = ?
                WHERE id = ? AND status = 'running' AND active_worker_id = ?;
                """,
                (now, job_id, worker_id),
            )

    def cancel_job(self, chat_id: str, job_id: str) -> AnalysisJobResponse:
        """Cancel an in-progress analysis job safely and save checkpoint."""
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            job = conn.execute(
                "SELECT status FROM analysis_jobs WHERE id = ? AND chat_id = ?;",
                (job_id, chat_id),
            ).fetchone()
            if not job:
                raise ValueError(f"Job '{job_id}' not found for chat '{chat_id}'.")

            if job["status"] in ("pending", "running"):
                conn.execute(
                    """
                    UPDATE analysis_jobs
                    SET status = 'paused_cancelled',
                        active_worker_id = NULL,
                        updated_at = ?
                    WHERE id = ?;
                    """,
                    (now, job_id),
                )
                conn.execute(
                    """
                    UPDATE analysis_chunks
                    SET status = 'pending', claimed_worker_id = NULL
                    WHERE job_id = ? AND status = 'running';
                    """,
                    (job_id,),
                )

        return self.get_job(chat_id, job_id)

    def get_latest_profile(self, chat_id: str) -> ProfileResponse | None:
        """Fetch the active current profile for a chat, including version-consistent memes."""
        with self.db.connection() as conn:
            row = conn.execute(
                """
                SELECT * FROM profiles WHERE chat_id = ? AND is_active = 1
                ORDER BY version DESC LIMIT 1;
                """,
                (chat_id,),
            ).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT * FROM profiles WHERE chat_id = ? ORDER BY version DESC LIMIT 1;",
                    (chat_id,),
                ).fetchone()
            if not row:
                return None

            meme_rows = conn.execute(
                """
                SELECT * FROM memes WHERE profile_id = ? ORDER BY label ASC;
                """,
                (row["id"],),
            ).fetchall()

            memes: list[MemeFinding] = []
            for mr in meme_rows:
                eids = []
                with contextlib.suppress(Exception):
                    eids = json.loads(mr["evidence_message_ids"])
                memes.append(
                    MemeFinding(
                        id=mr["id"],
                        chat_id=mr["chat_id"],
                        label=mr["label"],
                        description=mr["description"],
                        usage_context=mr["usage_context"],
                        evidence_message_ids=eids,
                        confidence=mr["confidence"] if mr["confidence"] is not None else 1.0,
                        approval_state=mr["approval_state"],
                    )
                )

            return ProfileResponse(
                id=row["id"],
                chat_id=row["chat_id"],
                version=row["version"],
                source_job_id=row["source_job_id"],
                instructions_md=row["instructions_md"],
                structured=json.loads(row["structured_json"]) if row["structured_json"] else {},
                memes=memes,
                edited_by_user=bool(row["edited_by_user"]),
                is_active=bool(row["is_active"]),
                created_at=row["created_at"],
            )

    def update_profile(
        self,
        chat_id: str,
        expected_version: int,
        instructions_md: str,
        structured: dict[str, Any] | None = None,
        memes_update: list[Any] | None = None,
    ) -> ProfileResponse:
        """
        Update profile with optimistic locking: rejects stale writes.
        Creates a new version with edited_by_user=1, sets is_active=1,
        and version-consistently copies memes to the new profile version.
        Rejects unknown or historical meme IDs not belonging to expected_version.
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            current = conn.execute(
                """
                SELECT * FROM profiles WHERE chat_id = ? AND is_active = 1
                ORDER BY version DESC LIMIT 1;
                """,
                (chat_id,),
            ).fetchone()
            if not current:
                current = conn.execute(
                    "SELECT * FROM profiles WHERE chat_id = ? ORDER BY version DESC LIMIT 1;",
                    (chat_id,),
                ).fetchone()

            current_version = current["version"] if current else 0
            if expected_version != current_version:
                raise ValueError(
                    f"Version conflict: expected {expected_version} != current {current_version}."
                )

            if current:
                current_meme_rows = conn.execute(
                    "SELECT * FROM memes WHERE profile_id = ? ORDER BY label ASC;",
                    (current["id"],),
                ).fetchall()
            else:
                current_meme_rows = []

            current_memes_by_id = {m["id"]: dict(m) for m in current_meme_rows}

            # Scope meme edits strictly to expected active profile; reject stale/historical IDs
            updates_by_id: dict[str, dict[str, Any]] = {}
            if memes_update:
                for mu in memes_update:
                    mu_dict = mu if isinstance(mu, dict) else mu.model_dump(exclude_unset=True)
                    mid = mu_dict.get("id")
                    if not mid:
                        continue
                    if mid not in current_memes_by_id:
                        raise ValueError(
                            f"Meme ID '{mid}' does not belong to expected profile version "
                            f"{expected_version}."
                        )
                    updates_by_id[mid] = mu_dict

            max_ver_row = conn.execute(
                "SELECT coalesce(max(version), 0) FROM profiles WHERE chat_id = ?;",
                (chat_id,),
            ).fetchone()
            new_version = (max_ver_row[0] if max_ver_row else 0) + 1

            new_id = str(uuid.uuid4())
            curr_struct = (
                json.loads(current["structured_json"])
                if current and current["structured_json"]
                else {}
            )
            struct_data = structured if structured is not None else curr_struct
            if hasattr(struct_data, "model_dump"):
                struct_data = struct_data.model_dump()

            # Deactivate older profiles
            conn.execute(
                "UPDATE profiles SET is_active = 0 WHERE chat_id = ?;",
                (chat_id,),
            )

            conn.execute(
                """
                INSERT INTO profiles (
                    id, chat_id, version, source_job_id,
                    instructions_md, structured_json, edited_by_user, is_active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?);
                """,
                (
                    new_id,
                    chat_id,
                    new_version,
                    current["source_job_id"] if current else None,
                    instructions_md,
                    json.dumps(struct_data, ensure_ascii=False),
                    now,
                ),
            )

            # Copy memes to the new profile version (preserving historical snapshots)
            for orig_meme in current_meme_rows:
                orig_id = orig_meme["id"]
                upd = updates_by_id.get(orig_id, {})
                label = upd.get("label") if upd.get("label") is not None else orig_meme["label"]
                description = (
                    upd.get("description")
                    if upd.get("description") is not None
                    else orig_meme["description"]
                )
                usage_context = (
                    upd.get("usage_context")
                    if upd.get("usage_context") is not None
                    else orig_meme["usage_context"]
                )
                approval_state = (
                    upd.get("approval_state")
                    if upd.get("approval_state") is not None
                    else orig_meme["approval_state"]
                )
                evidence_json = orig_meme["evidence_message_ids"]
                confidence = orig_meme["confidence"]

                new_meme_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO memes (
                        id, chat_id, profile_id, label, description, usage_context,
                        evidence_message_ids, confidence, approval_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        new_meme_id,
                        chat_id,
                        new_id,
                        label,
                        description,
                        usage_context,
                        evidence_json,
                        confidence,
                        approval_state,
                    ),
                )

        latest = self.get_latest_profile(chat_id)
        if not latest:
            raise RuntimeError("Failed to fetch newly created profile version.")
        return latest

    def export_profile_markdown(self, chat_id: str) -> str:
        """Export credential-free markdown instructions and approved memes."""
        with self.db.connection() as conn:
            chat = conn.execute("SELECT name FROM chats WHERE id = ?;", (chat_id,)).fetchone()
            chat_name = chat["name"] if chat else "Telegram Chat"

        profile = self.get_latest_profile(chat_id)
        if not profile:
            raise ValueError(f"No profile generated yet for chat '{chat_id}'.")

        lines = [
            f"# {chat_name} — Humor Profile and Bot Instructions",
            f"Exported at: {datetime.now(UTC).isoformat()}",
            f"Profile Version: {profile.version}",
            f"Edited by Owner: {'Yes' if profile.edited_by_user else 'No'}",
            "",
            "---",
            "",
            profile.instructions_md,
            "",
            "## Approved Memes and Inside Jokes",
        ]

        approved_memes = [m for m in profile.memes if m.approval_state == "approved"]
        if approved_memes:
            for m in approved_memes:
                lines.append(f"- **{m.label}**: {m.description}")
                if m.usage_context:
                    lines.append(f"  - Context: {m.usage_context}")
                if m.evidence_message_ids:
                    ids_str = ", ".join(str(i) for i in m.evidence_message_ids)
                    lines.append(f"  - Evidence Message IDs: {ids_str}")
        else:
            lines.append("No approved memes recorded.")

        return "\n".join(lines)

    def get_suggested_profile(self, chat_id: str) -> ProfileResponse | None:
        """Fetch the latest stored AI profile suggestion (inactive AI-generated profile), if any."""
        with self.db.connection() as conn:
            # Active profile version
            active_row = conn.execute(
                """
                SELECT version FROM profiles WHERE chat_id = ? AND is_active = 1
                ORDER BY version DESC LIMIT 1;
                """,
                (chat_id,),
            ).fetchone()
            active_version = active_row["version"] if active_row else 0

            # Latest inactive profile generated by AI (edited_by_user = 0) with higher version
            row = conn.execute(
                """
                SELECT * FROM profiles
                WHERE chat_id = ? AND is_active = 0 AND edited_by_user = 0 AND version > ?
                ORDER BY version DESC LIMIT 1;
                """,
                (chat_id, active_version),
            ).fetchone()
            if not row:
                return None

            meme_rows = conn.execute(
                """
                SELECT * FROM memes WHERE profile_id = ? ORDER BY label ASC;
                """,
                (row["id"],),
            ).fetchall()

            memes: list[MemeFinding] = []
            for mr in meme_rows:
                eids = []
                with contextlib.suppress(Exception):
                    eids = json.loads(mr["evidence_message_ids"])
                memes.append(
                    MemeFinding(
                        id=mr["id"],
                        chat_id=mr["chat_id"],
                        label=mr["label"],
                        description=mr["description"],
                        usage_context=mr["usage_context"],
                        evidence_message_ids=eids,
                        confidence=mr["confidence"] if mr["confidence"] is not None else 1.0,
                        approval_state=mr["approval_state"],
                    )
                )

            return ProfileResponse(
                id=row["id"],
                chat_id=row["chat_id"],
                version=row["version"],
                source_job_id=row["source_job_id"],
                instructions_md=row["instructions_md"],
                structured=json.loads(row["structured_json"]) if row["structured_json"] else {},
                memes=memes,
                edited_by_user=bool(row["edited_by_user"]),
                is_active=bool(row["is_active"]),
                created_at=row["created_at"],
            )

    def apply_suggested_profile(
        self,
        chat_id: str,
        expected_active_version: int,
        suggestion_id: str | None = None,
    ) -> ProfileResponse:
        """
        Explicitly apply a stored AI profile suggestion with optimistic concurrency.
        Guards against version conflict: requires expected_active_version to match
        current active profile version.
        Creates a new active profile version marked edited_by_user=1 (owner applied it),
        leaving historical profiles and meme snapshots intact.
        """
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            active = conn.execute(
                """
                SELECT * FROM profiles WHERE chat_id = ? AND is_active = 1
                ORDER BY version DESC LIMIT 1;
                """,
                (chat_id,),
            ).fetchone()
            curr_active_version = active["version"] if active else 0
            if expected_active_version != curr_active_version:
                raise ValueError(
                    f"Version conflict: expected active version {expected_active_version} "
                    f"!= current {curr_active_version}."
                )

            if suggestion_id:
                sugg = conn.execute(
                    "SELECT * FROM profiles WHERE id = ? AND chat_id = ?;",
                    (suggestion_id, chat_id),
                ).fetchone()
            else:
                sugg = conn.execute(
                    """
                    SELECT * FROM profiles
                    WHERE chat_id = ? AND is_active = 0 AND edited_by_user = 0
                    ORDER BY version DESC LIMIT 1;
                    """,
                    (chat_id,),
                ).fetchone()

            if not sugg:
                raise ValueError(f"No AI profile suggestion found for chat '{chat_id}'.")

            sugg_memes = conn.execute(
                "SELECT * FROM memes WHERE profile_id = ? ORDER BY label ASC;",
                (sugg["id"],),
            ).fetchall()

            max_ver_row = conn.execute(
                "SELECT coalesce(max(version), 0) FROM profiles WHERE chat_id = ?;",
                (chat_id,),
            ).fetchone()
            new_version = (max_ver_row[0] if max_ver_row else 0) + 1
            new_id = str(uuid.uuid4())

            # Deactivate older active profiles
            conn.execute("UPDATE profiles SET is_active = 0 WHERE chat_id = ?;", (chat_id,))

            conn.execute(
                """
                INSERT INTO profiles (
                    id, chat_id, version, source_job_id,
                    instructions_md, structured_json, edited_by_user, is_active, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, 1, ?);
                """,
                (
                    new_id,
                    chat_id,
                    new_version,
                    sugg["source_job_id"],
                    sugg["instructions_md"],
                    sugg["structured_json"],
                    now,
                ),
            )

            # Copy memes from suggestion with fresh UUIDs
            for sm in sugg_memes:
                conn.execute(
                    """
                    INSERT INTO memes (
                        id, chat_id, profile_id, label, description, usage_context,
                        evidence_message_ids, confidence, approval_state
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        str(uuid.uuid4()),
                        chat_id,
                        new_id,
                        sm["label"],
                        sm["description"],
                        sm["usage_context"],
                        sm["evidence_message_ids"],
                        sm["confidence"],
                        sm["approval_state"],
                    ),
                )

        latest = self.get_latest_profile(chat_id)
        if not latest:
            raise RuntimeError("Failed to fetch newly created active profile.")
        return latest
