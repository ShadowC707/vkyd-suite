"""Typed async provider interface and structured data contracts."""

from abc import ABC, abstractmethod
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field


class ProviderError(Exception):
    """Sanitized provider error that never leaks API credentials or request headers."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool = False,
        status_code: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status_code = status_code


class MemeCandidate(BaseModel):
    label: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=2000)
    usage_context: str = Field(default="", max_length=2000)
    evidence_source_message_ids: list[int] = Field(default_factory=list, max_length=100)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)


class StyleAnalysis(BaseModel):
    languages: list[Annotated[str, Field(max_length=100)]] = Field(
        default_factory=lambda: ["Ukrainian"], max_length=20
    )
    reply_length: str = Field(default="short and punchy", max_length=500)
    tone_and_register: str = Field(default="informal, humorous, friendly", max_length=1000)
    humor_techniques: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=50
    )
    recurring_themes: list[Annotated[str, Field(max_length=500)]] = Field(
        default_factory=list, max_length=50
    )


class ChunkAnalysisResult(BaseModel):
    style: StyleAnalysis = Field(default_factory=StyleAnalysis)
    memes: list[MemeCandidate] = Field(default_factory=list, max_length=100)
    instructions_summary: str = Field(default="", max_length=20000)


class MergeProfileResult(BaseModel):
    instructions_md: str = Field(min_length=1, max_length=20000)
    structured: StyleAnalysis = Field(default_factory=StyleAnalysis)
    memes: list[MemeCandidate] = Field(default_factory=list, max_length=100)


class ProviderReplyDecision(BaseModel):
    action: Literal["quote", "generate", "silent"] = Field(
        default="quote", description="'quote', 'generate', 'silent'"
    )
    quote_source_message_id: int | None = None
    text: str | None = Field(default=None, max_length=4000)
    evidence_source_message_ids: list[int] = Field(default_factory=list, max_length=100)


class BaseAIProvider(ABC):
    """Abstract async provider interface for chat analysis and reply generation."""

    @property
    @abstractmethod
    def provider_kind(self) -> str:
        """Name of the provider implementation (e.g. 'gemini', 'fake')."""
        ...

    @abstractmethod
    async def analyze_chunk(
        self,
        messages: list[dict[str, Any]],
        chat_name: str,
        max_output_tokens: int | None = None,
    ) -> ChunkAnalysisResult:
        """Analyze a bounded chunk of supported text messages."""
        ...

    @abstractmethod
    async def merge_findings(
        self,
        chunk_findings: list[ChunkAnalysisResult],
        current_profile: dict[str, Any] | None,
        chat_name: str,
    ) -> MergeProfileResult:
        """Merge findings from multiple chunks into a unified profile."""
        ...

    @abstractmethod
    async def decide_reply(
        self,
        context: str,
        candidate_quotes: list[dict[str, Any]],
        profile_instructions: str,
        mode: str,
    ) -> ProviderReplyDecision:
        """Decide an in-character reply (quote, generate, or silent)."""
        ...

    @abstractmethod
    async def smoke_test(self) -> dict[str, Any]:
        """Opt-in tiny synthetic smoke test verifying provider readiness without logging secrets."""
        ...

    async def close(self) -> None:
        """Close provider resources, HTTP clients, and background executors."""
        return None
