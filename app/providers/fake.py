"""Deterministic test-only fake AI provider."""

from collections.abc import Callable, Coroutine
from typing import Any, Literal

from app.providers.base import (
    BaseAIProvider,
    ChunkAnalysisResult,
    MemeCandidate,
    MergeProfileResult,
    ProviderError,
    ProviderReplyDecision,
    StyleAnalysis,
)


class FakeAIProvider(BaseAIProvider):
    """Test-only deterministic AI provider with programmable failure modes."""

    def __init__(
        self,
        should_fail: bool = False,
        error_to_raise: ProviderError | None = None,
        fabricated_evidence_ids: list[int] | None = None,
        fixed_quote_id: int | None = None,
        fixed_action: Literal["quote", "generate", "silent"] | None = None,
        fixed_text: str | None = None,
        return_empty_response: bool = False,
        max_envelope_chars: int | None = None,
        max_findings_per_merge: int | None = None,
        on_analyze_chunk: Callable[[], Coroutine[Any, Any, None]] | None = None,
        on_merge_findings: Callable[[], Coroutine[Any, Any, None]] | None = None,
        on_decide_reply: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        self.should_fail = should_fail
        self.error_to_raise = error_to_raise
        self.fabricated_evidence_ids = fabricated_evidence_ids
        self.fixed_quote_id = fixed_quote_id
        self.fixed_action = fixed_action
        self.fixed_text = fixed_text
        self.return_empty_response = return_empty_response
        self.max_envelope_chars = max_envelope_chars
        self.max_findings_per_merge = max_findings_per_merge
        self.on_analyze_chunk = on_analyze_chunk
        self.on_merge_findings = on_merge_findings
        self.on_decide_reply = on_decide_reply
        self.call_count: int = 0
        self.recorded_merge_batch_sizes: list[int] = []

    @property
    def provider_kind(self) -> str:
        return "fake"

    def _check_error(self) -> None:
        self.call_count += 1
        if self.should_fail:
            if self.error_to_raise:
                raise self.error_to_raise
            raise ProviderError(
                code="provider_error",
                message="Deterministic fake provider simulated failure.",
            )
        if self.return_empty_response:
            raise ProviderError(
                code="invalid_response",
                message="Fake provider returned empty output.",
            )

    async def analyze_chunk(
        self,
        messages: list[dict[str, Any]],
        chat_name: str,
        max_output_tokens: int | None = None,
    ) -> ChunkAnalysisResult:
        if self.on_analyze_chunk:
            await self.on_analyze_chunk()

        if self.max_envelope_chars is not None:
            # Measure full serialized line lengths including sender names and ID formatting
            formatted_lines = [
                f"[ID: {m.get('source_message_id')}] "
                f"{(m.get('sender_name') or 'Participant')[:100]}: {m.get('text', '')}"
                for m in messages
            ]
            total_chars = sum(len(line) for line in formatted_lines)
            if total_chars > self.max_envelope_chars:
                raise ProviderError(
                    code="envelope_exceeded",
                    message=(
                        f"Chunk serialized text ({total_chars} chars) exceeds maximum envelope "
                        f"({self.max_envelope_chars} chars)."
                    ),
                )

        self._check_error()

        # Extract real evidence IDs from chunk messages
        real_ids = [
            msg["source_message_id"]
            for msg in messages
            if "source_message_id" in msg and msg.get("is_supported_text", 1) == 1
        ]
        if self.fabricated_evidence_ids is not None:
            evidence_ids = self.fabricated_evidence_ids
        else:
            evidence_ids = real_ids[:3]

        meme = MemeCandidate(
            label=f"Meme from {chat_name}",
            description="Recurring joke observed in message chunk.",
            usage_context="Used during conversational banter.",
            evidence_source_message_ids=evidence_ids,
            confidence=0.9,
        )

        style = StyleAnalysis(
            languages=["Ukrainian", "Surzhyk"],
            reply_length="short and punchy",
            tone_and_register="witty, sarcastic",
            humor_techniques=["irony", "callbacks"],
            recurring_themes=["daily routine", "humor"],
        )

        return ChunkAnalysisResult(
            style=style,
            memes=[meme],
            instructions_summary="Speak informally, punchy one-liners, inside jokes.",
        )

    async def merge_findings(
        self,
        chunk_findings: list[ChunkAnalysisResult],
        current_profile: dict[str, Any] | None,
        chat_name: str,
    ) -> MergeProfileResult:
        if self.on_merge_findings:
            await self.on_merge_findings()

        self.recorded_merge_batch_sizes.append(len(chunk_findings))

        if (
            self.max_findings_per_merge is not None
            and len(chunk_findings) > self.max_findings_per_merge
        ):
            raise ProviderError(
                code="merge_envelope_exceeded",
                message=(
                    f"Merge received {len(chunk_findings)} findings, exceeding limit of "
                    f"{self.max_findings_per_merge}."
                ),
            )

        self._check_error()

        # Collect and deduplicate memes
        all_memes: list[MemeCandidate] = []
        seen_labels: set[str] = set()

        for chunk in chunk_findings:
            for m in chunk.memes:
                if m.label not in seen_labels:
                    seen_labels.add(m.label)
                    all_memes.append(m)

        instructions_md = (
            f"# {chat_name} Humor Bot Instructions\n\n"
            "## Persona\n"
            "You are a witty companion representing the humor of this chat.\n\n"
            "## Rules\n"
            "1. Reply in Ukrainian/Surzhyk using punchy sentences.\n"
            "2. When quoting, only select genuine quotes from the candidates.\n"
            "3. Keep jokes friendly and contextual.\n"
        )

        style = StyleAnalysis(
            languages=["Ukrainian", "Surzhyk"],
            reply_length="short and punchy",
            tone_and_register="informal, humorous, friendly",
            humor_techniques=["callbacks", "irony"],
            recurring_themes=["daily routine"],
        )

        return MergeProfileResult(
            instructions_md=instructions_md,
            structured=style,
            memes=all_memes,
        )

    async def decide_reply(
        self,
        context: str,
        candidate_quotes: list[dict[str, Any]],
        profile_instructions: str,
        mode: str,
    ) -> ProviderReplyDecision:
        if self.on_decide_reply:
            await self.on_decide_reply()

        self._check_error()

        if self.fixed_action:
            action: Literal["quote", "generate", "silent"] = self.fixed_action
        elif mode == "quote":
            action = "quote"
        elif mode == "generate":
            action = "generate"
        else:  # auto
            action = "quote" if candidate_quotes else "generate"

        quote_id: int | None = None
        text: str | None = None
        evidence_ids: list[int] = []

        if action == "quote":
            if self.fixed_quote_id is not None:
                quote_id = self.fixed_quote_id
            elif candidate_quotes:
                quote_id = candidate_quotes[0].get("source_message_id")
            evidence_ids = [quote_id] if quote_id is not None else []
        elif action == "generate":
            text = (
                self.fixed_text
                if self.fixed_text is not None
                else "Оце так поворот! Точно як минулого разу."
            )
            if candidate_quotes:
                evidence_ids = [candidate_quotes[0].get("source_message_id", 0)]
        elif action == "silent":
            text = None

        return ProviderReplyDecision(
            action=action,
            quote_source_message_id=quote_id,
            text=text,
            evidence_source_message_ids=evidence_ids,
        )

    async def smoke_test(self) -> dict[str, Any]:
        self._check_error()
        return {
            "status": "ok",
            "model": "fake-model",
            "verified": True,
            "preview": "ok",
        }

    async def close(self) -> None:
        pass
