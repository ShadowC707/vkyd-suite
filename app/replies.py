"""Grounded reply service and feedback persistence."""

import json
import uuid
from datetime import UTC, datetime

from app.analysis import AnalysisService
from app.db import Database
from app.models import FeedbackResponse, ReplyRequest, ReplyResponse, ResolvedQuote
from app.providers.base import BaseAIProvider, ProviderError
from app.retrieval import MemoryRetriever


class ReplyService:
    """Manages grounded replies (quote-only, generated, auto/silent) and feedback persistence."""

    def __init__(
        self,
        db: Database,
        retriever: MemoryRetriever,
        analysis_service: AnalysisService,
    ) -> None:
        self.db = db
        self.retriever = retriever
        self.analysis_service = analysis_service

    async def generate_reply(
        self,
        chat_id: str,
        request: ReplyRequest,
        provider: BaseAIProvider | None = None,
    ) -> ReplyResponse:
        with self.db.connection() as conn:
            chat = conn.execute("SELECT id, name FROM chats WHERE id = ?;", (chat_id,)).fetchone()
            if not chat:
                raise ValueError(f"Chat '{chat_id}' not found.")

        # 1. Fetch style profile instructions (or default fallback)
        profile = self.analysis_service.get_latest_profile(chat_id)
        instructions_md = (
            profile.instructions_md
            if profile
            else "You are a witty companion for this group chat. Be punchy and natural."
        )

        # 2. Retrieve candidate quotes with feedback-aware ranking
        candidate_quotes = self.retriever.get_candidate_quotes(
            chat_id=chat_id,
            query=request.context,
            limit=5,
            excluded_quote_ids=request.excluded_quote_ids,
        )

        action: str
        quote_id: str | None = None
        resolved_quote: ResolvedQuote | None = None
        text: str | None = None
        evidence_ids: list[int] = []
        provider_kind: str

        # 3. Handle Mode
        if request.mode == "quote":
            # Quote-only mode requires no AI provider
            provider_kind = "deterministic"
            if candidate_quotes:
                top = candidate_quotes[0]
                with self.db.connection() as conn:
                    row = conn.execute(
                        """
                        SELECT id, source_message_id, sender_name, text, sent_at
                        FROM messages WHERE id = ? AND chat_id = ?;
                        """,
                        (top["id"], chat_id),
                    ).fetchone()
                if row:
                    action = "quote"
                    quote_id = row["id"]
                    resolved_quote = ResolvedQuote(
                        id=row["id"],
                        source_message_id=row["source_message_id"],
                        sender_name=row["sender_name"],
                        text=row["text"],
                        sent_at=row["sent_at"],
                    )
                    evidence_ids = [row["source_message_id"]]
                else:
                    action = "silent"
            else:
                action = "silent"

        elif request.mode in ("generate", "auto"):
            if provider is None:
                raise ProviderError(
                    code="credential_missing",
                    message=f"Provider required for '{request.mode}' mode but is not configured.",
                    retryable=False,
                    status_code=400,
                )
            provider_kind = provider.provider_kind

            decision = await provider.decide_reply(
                context=request.context,
                candidate_quotes=candidate_quotes,
                profile_instructions=instructions_md,
                mode=request.mode,
            )

            if decision.action == "quote":
                # Validate quote ID server-side from candidate quotes within this chat
                chosen_source_id = decision.quote_source_message_id
                target_cand = next(
                    (q for q in candidate_quotes if q["source_message_id"] == chosen_source_id),
                    None,
                )

                if target_cand is None:
                    # Fail-closed: model selected an unverified or fabricated quote ID.
                    # Do NOT author or guess a quote; safely fail closed to silent.
                    action = "silent"
                else:
                    with self.db.connection() as conn:
                        row = conn.execute(
                            """
                            SELECT id, source_message_id, sender_name, text, sent_at
                            FROM messages WHERE id = ? AND chat_id = ?;
                            """,
                            (target_cand["id"], chat_id),
                        ).fetchone()
                    if row:
                        action = "quote"
                        quote_id = row["id"]
                        resolved_quote = ResolvedQuote(
                            id=row["id"],
                            source_message_id=row["source_message_id"],
                            sender_name=row["sender_name"],
                            text=row["text"],
                            sent_at=row["sent_at"],
                        )
                        evidence_ids = [row["source_message_id"]]
                    else:
                        action = "silent"

            elif decision.action == "generate":
                stripped = (decision.text or "").strip()
                if not stripped:
                    # Empty or whitespace output must not become a sendable generate action
                    action = "silent"
                    text = None
                else:
                    action = "generate"
                    # Bound generated text length to 500 characters
                    text = stripped[:500]
                    with self.db.connection() as conn:
                        valid_source_ids = {
                            r[0]
                            for r in conn.execute(
                                "SELECT source_message_id FROM messages WHERE chat_id = ?;",
                                (chat_id,),
                            ).fetchall()
                        }
                    evidence_ids = [
                        eid
                        for eid in decision.evidence_source_message_ids
                        if eid in valid_source_ids
                    ]

            else:  # silent
                action = "silent"
                text = None

        else:
            raise ValueError(f"Unsupported reply mode: '{request.mode}'.")

        # 4. Persist reply to database
        reply_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO replies (
                    id, chat_id, mode, action, text, quote_id,
                    sticker_id, evidence_ids_json, provider_kind, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?, ?);
                """,
                (
                    reply_id,
                    chat_id,
                    request.mode,
                    action,
                    text,
                    quote_id,
                    json.dumps(evidence_ids),
                    provider_kind,
                    now,
                ),
            )

        return ReplyResponse(
            id=reply_id,
            chat_id=chat_id,
            mode=request.mode,
            action=action,
            text=text,
            quote=resolved_quote,
            sticker_id=None,
            evidence_ids=evidence_ids,
            provider_kind=provider_kind,
            created_at=now,
        )

    def record_feedback(
        self,
        chat_id: str,
        reply_id: str,
        verdict: str,
    ) -> FeedbackResponse:
        """
        Record feedback for a reply belonging to this chat.
        Idempotent/repeat handling: updates existing feedback for this reply rather
        than compounding duplicate feedback entries.
        """
        allowed_verdicts = {"landed", "missed", "overused"}
        if verdict not in allowed_verdicts:
            raise ValueError(
                f"Invalid verdict '{verdict}'. Allowed values: {sorted(allowed_verdicts)}."
            )

        now = datetime.now(UTC).isoformat()
        with self.db.transaction() as conn:
            reply = conn.execute(
                "SELECT id FROM replies WHERE id = ? AND chat_id = ?;",
                (reply_id, chat_id),
            ).fetchone()
            if not reply:
                raise ValueError(f"Reply '{reply_id}' not found for chat '{chat_id}'.")

            existing = conn.execute(
                "SELECT id FROM feedback WHERE reply_id = ? AND chat_id = ?;",
                (reply_id, chat_id),
            ).fetchone()

            if existing:
                feedback_id = existing["id"]
                conn.execute(
                    "UPDATE feedback SET verdict = ?, created_at = ? WHERE id = ?;",
                    (verdict, now, feedback_id),
                )
            else:
                feedback_id = str(uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO feedback (id, reply_id, chat_id, verdict, created_at)
                    VALUES (?, ?, ?, ?, ?);
                    """,
                    (feedback_id, reply_id, chat_id, verdict, now),
                )

        return FeedbackResponse(
            id=feedback_id,
            reply_id=reply_id,
            chat_id=chat_id,
            verdict=verdict,
            created_at=now,
        )
