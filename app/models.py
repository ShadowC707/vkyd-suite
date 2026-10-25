"""Pydantic models and API schemas conforming to docs/contracts.md."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "0.1.0"
    database_ready: bool
    provider_configured: bool
    telegram_configured: bool


class ImportReport(BaseModel):
    total_records: int = 0
    supported_text_messages: int = 0
    service_records: int = 0
    empty_messages: int = 0
    media_only_messages: int = 0
    unsupported_media_count: int = 0
    stickers_count: int = 0
    duplicate_records: int = 0
    earliest_sent_at: str | None = None
    latest_sent_at: str | None = None


class ChatSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    source_chat_id: str | None = None
    corpus_version: int = 1
    import_fingerprint: str
    created_at: str
    import_stats: ImportReport
    total_messages: int = 0
    supported_text_messages: int = 0
    analyzed_messages: int = 0


class ChatDetail(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    source_chat_id: str | None = None
    corpus_version: int = 1
    import_fingerprint: str
    created_at: str
    import_stats: ImportReport
    total_messages: int = 0
    supported_text_messages: int = 0
    analyzed_messages: int = 0
    coverage: dict[str, Any] = Field(default_factory=dict)
    latest_job: AnalysisJobResponse | None = None


class ImportResponse(BaseModel):
    chat: ChatSummary
    report: ImportReport


class ContextMessage(BaseModel):
    id: str
    source_message_id: int
    sender_name: str | None = None
    text: str = ""
    sent_at: str | None = None
    is_target: bool = False


class MemoryItem(BaseModel):
    id: str
    chat_id: str
    source_message_id: int
    sender_id: str | None = None
    sender_name: str | None = None
    sent_at: str | None = None
    raw_date: str = ""
    kind: str
    text: str
    reply_to_source_id: int | None = None
    sticker_metadata: dict[str, Any] | None = None
    reply_parent: ContextMessage | None = None
    nearby_context: list[ContextMessage] = Field(default_factory=list)


class PaginatedResponse[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
    query: str | None = None


# --- Analysis Models ---


class AnalysisEstimateResponse(BaseModel):
    chat_id: str
    total_supported_messages: int
    estimated_chunks: int
    estimated_merge_calls: int = 0
    estimated_total_calls: int = 0
    chunk_size: int
    context_overlap: int
    max_calls: int
    provider_configured: bool
    merge_estimate_note: str = (
        "Merge calls estimate is a lower bound based on fan-in; "
        "data-dependent findings size may require additional merge levels."
    )


class AnalysisStartRequest(BaseModel):
    max_calls: int | None = Field(default=None, ge=1, le=5000)
    chunk_size: int | None = Field(default=None, ge=10, le=1000)
    context_overlap: int | None = Field(default=None, ge=0, le=50)


class ProfileApplySuggestionRequest(BaseModel):
    expected_active_version: int
    suggestion_id: str | None = None


class AnalysisJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chat_id: str
    corpus_version: int
    status: Literal[
        "pending",
        "running",
        "completed",
        "paused_budget",
        "paused_cancelled",
        "failed",
    ]
    total_supported_messages: int
    processed_unique_messages: int
    total_chunks: int
    completed_chunks: int
    provider_calls: int
    max_calls: int
    chunk_size: int = 50
    context_overlap: int = 10
    last_error_code: str | None = None
    can_resume: bool = False
    created_at: str
    updated_at: str


# --- Profile Models ---


class MemeFinding(BaseModel):
    id: str
    chat_id: str
    label: str = Field(min_length=1, max_length=150)
    description: str = Field(min_length=1, max_length=500)
    usage_context: str | None = Field(default=None, max_length=500)
    evidence_message_ids: list[int] = Field(default_factory=list, max_length=50)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    approval_state: Literal["approved", "unverified", "rejected"] = "approved"


class MemeFindingUpdate(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    label: str | None = Field(default=None, max_length=150)
    description: str | None = Field(default=None, max_length=500)
    usage_context: str | None = Field(default=None, max_length=500)
    approval_state: Literal["approved", "unverified", "rejected"] | None = None


class ProfileResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chat_id: str
    version: int
    source_job_id: str | None = None
    instructions_md: str
    structured: dict[str, Any] = Field(default_factory=dict)
    memes: list[MemeFinding] = Field(default_factory=list)
    edited_by_user: bool = False
    is_active: bool = True
    created_at: str


class ProfileUpdateRequest(BaseModel):
    expected_version: int
    instructions_md: str = Field(min_length=1, max_length=10000)
    structured: dict[str, Any] | None = None
    memes: list[MemeFindingUpdate] | None = Field(default=None, max_length=100)

    @field_validator("structured", mode="before")
    @classmethod
    def validate_structured(cls, v: Any) -> Any:
        if v is None:
            return None
        if hasattr(v, "model_dump"):
            v = v.model_dump()
        if isinstance(v, dict):
            serialized = json.dumps(v, ensure_ascii=False)
            if len(serialized) > 5000:
                raise ValueError("Structured profile dictionary exceeds 5000 characters limit.")
            return v
        raise ValueError("structured must be a bounded dictionary.")


# --- Reply Models ---


class ReplyRequest(BaseModel):
    context: str = Field(min_length=1, max_length=4000)
    mode: Literal["quote", "generate", "auto"] = "auto"
    excluded_quote_ids: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=list, max_length=100
    )


class ResolvedQuote(BaseModel):
    id: str
    source_message_id: int
    sender_name: str | None = None
    text: str
    sent_at: str | None = None


class ReplyResponse(BaseModel):
    id: str
    chat_id: str
    mode: Literal["quote", "generate", "auto"]
    action: Literal["quote", "generate", "silent"]
    text: str | None = Field(default=None, max_length=1000)
    quote: ResolvedQuote | None = None
    sticker_id: str | None = None
    evidence_ids: list[int] = Field(default_factory=list, max_length=50)
    provider_kind: str
    created_at: str


# --- Feedback Models ---


class FeedbackRequest(BaseModel):
    verdict: Literal["landed", "missed", "overused"]


class FeedbackResponse(BaseModel):
    id: str
    reply_id: str
    chat_id: str
    verdict: Literal["landed", "missed", "overused"]
    created_at: str


# --- Telegram Models ---


class TelegramBindingItem(BaseModel):
    telegram_chat_id: str
    chat_id: str
    chat_name: str | None = None
    enabled: bool = True
    cooldown_seconds: int = 10
    last_sent_at: str | None = None


class TelegramStatusResponse(BaseModel):
    configured: bool
    bot_username: str | None = None
    allowed_chat_ids: list[str] = Field(default_factory=list)
    bindings: list[TelegramBindingItem] = Field(default_factory=list)


class TelegramBindingUpdateRequest(BaseModel):
    chat_id: str
    enabled: bool = True
    cooldown_seconds: int = Field(default=10, ge=1, le=3600)
