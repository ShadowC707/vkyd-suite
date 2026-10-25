"""Tests for bounded async worker pool: concurrency, budget caps, error bounds, and resume."""

import asyncio

import pytest

from app.analysis import AnalysisService
from app.db import Database
from app.providers.base import (
    BaseAIProvider,
    ChunkAnalysisResult,
    MemeCandidate,
    MergeProfileResult,
    ProviderError,
    ProviderReplyDecision,
    StyleAnalysis,
)


class ConcurrencySpyProvider(BaseAIProvider):
    """Spy provider tracking peak active concurrency, dispatched calls, and call history."""

    def __init__(self, delay: float = 0.05, fail_on_chunk: int | None = None) -> None:
        self.delay = delay
        self.fail_on_chunk = fail_on_chunk
        self.active_concurrency = 0
        self.peak_concurrency = 0
        self.total_calls = 0
        self.dispatched_chunks: list[int] = []
        self._lock = asyncio.Lock()

    @property
    def provider_kind(self) -> str:
        return "concurrency_spy"

    async def analyze_chunk(
        self,
        messages: list[dict],
        chat_name: str,
        max_output_tokens: int | None = None,
    ) -> ChunkAnalysisResult:
        async with self._lock:
            self.total_calls += 1
            self.active_concurrency += 1
            if self.active_concurrency > self.peak_concurrency:
                self.peak_concurrency = self.active_concurrency
            src_ids = [m["source_message_id"] for m in messages]
            self.dispatched_chunks.append(src_ids[0])

        try:
            if self.fail_on_chunk is not None and self.fail_on_chunk in src_ids:
                raise ProviderError(
                    code="simulated_chunk_failure",
                    message=f"Simulated failure on chunk with message {self.fail_on_chunk}",
                )
            if self.delay > 0:
                await asyncio.sleep(self.delay)

            return ChunkAnalysisResult(
                style=StyleAnalysis(),
                memes=[
                    MemeCandidate(
                        label=f"Meme for chunk {src_ids[0]}",
                        description=f"Description for chunk {src_ids[0]}",
                        evidence_source_message_ids=[src_ids[0]],
                        confidence=0.9,
                    )
                ],
                instructions_summary=f"Summary for chunk {src_ids[0]}",
            )
        finally:
            async with self._lock:
                self.active_concurrency -= 1

    async def merge_findings(
        self,
        chunk_findings: list[ChunkAnalysisResult],
        current_profile: dict | None,
        chat_name: str,
        max_output_tokens: int | None = None,
    ) -> MergeProfileResult:
        return MergeProfileResult(
            instructions_md="Group humor guidelines.",
            structured=StyleAnalysis(),
            memes=[
                MemeCandidate(
                    label="Merged Meme",
                    description="Merged description",
                    evidence_source_message_ids=[1],
                    confidence=0.9,
                )
            ],
        )

    async def decide_reply(
        self,
        context: str,
        candidate_quotes: list[dict],
        profile_instructions: str,
        mode: str,
        max_output_tokens: int | None = None,
    ) -> ProviderReplyDecision:
        return ProviderReplyDecision(action="silent")

    async def smoke_test(self) -> dict:
        return {"status": "ok"}

    async def close(self) -> None:
        pass


def _setup_test_chat_with_messages(db: Database, chat_id: str, count: int = 16) -> None:
    """Helper creating a test chat with exact count of supported messages."""
    import json

    with db.transaction() as conn:
        stats = {
            "total_records": count,
            "supported_text_messages": count,
            "service_records": 0,
            "empty_messages": 0,
            "media_only_messages": 0,
            "unsupported_media_count": 0,
            "stickers_count": 0,
            "duplicate_records": 0,
            "earliest_sent_at": "2026-01-01T10:00:00Z",
            "latest_sent_at": "2026-01-01T11:00:00Z",
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES (?, 'Concurrency Test Chat', 1, ?, '2026-10-25', ?);
            """,
            (chat_id, f"fp_conc_{chat_id}", json.dumps(stats)),
        )
        for i in range(1, count + 1):
            conn.execute(
                """
                INSERT OR REPLACE INTO messages (
                    id, chat_id, source_message_id, sender_name,
                    sent_at, raw_date, kind, text, source_order, is_supported_text
                ) VALUES (?, ?, ?, 'Alice', '2026-01-01T10:00:00Z', '2026-01-01',
                        'message', ?, ?, 1);
                """,
                (f"m_{chat_id}_{i}", chat_id, i, f"Message {i} content text", i),
            )


@pytest.mark.anyio
async def test_bounded_worker_pool_executes_concurrently(tmp_path):
    """Verify that execute_job dispatches chunks concurrently using bounded pool."""
    db = Database(tmp_path / "test_concurrency.db")
    db.run_migrations()

    chat_id = "chat_concurrent_1"
    # 16 messages with chunk_size=1 -> 16 chunks
    _setup_test_chat_with_messages(db, chat_id, count=16)

    service = AnalysisService(db, chunk_size=1, overlap=0)
    job = service.create_or_resume_job(chat_id, max_calls=50, chunk_size=1, overlap=0)
    assert job.total_chunks == 16

    spy = ConcurrencySpyProvider(delay=0.08)
    # Run with concurrency=8
    final_job = await service.execute_job(chat_id, job.id, provider=spy, concurrency=8)

    assert final_job.status == "completed"
    assert final_job.completed_chunks == 16
    assert final_job.processed_unique_messages == 16
    # Peak concurrency must be > 1 (verifies parallel dispatch)
    assert spy.peak_concurrency >= 4, f"Expected peak concurrency >= 4, got {spy.peak_concurrency}"
    assert spy.peak_concurrency <= 8, f"Expected peak concurrency <= 8, got {spy.peak_concurrency}"


@pytest.mark.anyio
async def test_worker_pool_respects_lifetime_cap_and_pauses_honestly(tmp_path):
    """Verify worker pool stops when lifetime call cap is reached without exceeding it."""
    db = Database(tmp_path / "test_cap.db")
    db.run_migrations()

    chat_id = "chat_cap_1"
    _setup_test_chat_with_messages(db, chat_id, count=12)

    service = AnalysisService(db, chunk_size=1, overlap=0)
    # Set cap to 5 calls for 12 chunks
    job = service.create_or_resume_job(chat_id, max_calls=5, chunk_size=1, overlap=0)
    assert job.total_chunks == 12

    spy = ConcurrencySpyProvider(delay=0.05)
    result_job = await service.execute_job(chat_id, job.id, provider=spy, concurrency=6)

    assert result_job.status == "paused_budget"
    assert result_job.provider_calls == 5
    assert result_job.completed_chunks <= 5
    # Verify no chunks are left in 'running' state
    with db.connection() as conn:
        running_cnt = conn.execute(
            "SELECT count(*) FROM analysis_chunks WHERE job_id = ? AND status = 'running';",
            (job.id,),
        ).fetchone()[0]
        assert running_cnt == 0


@pytest.mark.anyio
async def test_worker_pool_stops_on_error_and_preserves_completed_chunks(tmp_path):
    """On error, worker pool stops claiming new chunks, finishes in-flight, and preserves work."""
    db = Database(tmp_path / "test_err.db")
    db.run_migrations()

    chat_id = "chat_err_1"
    _setup_test_chat_with_messages(db, chat_id, count=10)

    service = AnalysisService(db, chunk_size=1, overlap=0)
    job = service.create_or_resume_job(chat_id, max_calls=20, chunk_size=1, overlap=0)

    # Fail specifically on message 3
    spy = ConcurrencySpyProvider(delay=0.06, fail_on_chunk=3)
    with pytest.raises(ProviderError) as exc_info:
        await service.execute_job(chat_id, job.id, provider=spy, concurrency=4)

    assert exc_info.value.code == "simulated_chunk_failure"

    # Verify job is marked failed
    job_status = service.get_job(chat_id, job.id)
    assert job_status.status == "failed"
    assert job_status.last_error_code == "simulated_chunk_failure"

    # Verify no chunks are stuck in 'running'
    with db.connection() as conn:
        running_cnt = conn.execute(
            "SELECT count(*) FROM analysis_chunks WHERE job_id = ? AND status = 'running';",
            (job.id,),
        ).fetchone()[0]
        assert running_cnt == 0
        # Failed chunk exists
        failed_cnt = conn.execute(
            "SELECT count(*) FROM analysis_chunks WHERE job_id = ? AND status = 'failed';",
            (job.id,),
        ).fetchone()[0]
        assert failed_cnt >= 1


@pytest.mark.anyio
async def test_worker_pool_resumes_without_reanalyzing_completed_chunks(tmp_path):
    """Resuming paused job continues from pending chunks without reanalyzing completed ones."""
    db = Database(tmp_path / "test_resume.db")
    db.run_migrations()

    chat_id = "chat_resume_1"
    _setup_test_chat_with_messages(db, chat_id, count=8)

    service = AnalysisService(db, chunk_size=1, overlap=0)
    # Stage 1: Pause at cap=4
    job1 = service.create_or_resume_job(chat_id, max_calls=4, chunk_size=1, overlap=0)
    spy1 = ConcurrencySpyProvider(delay=0.03)
    res1 = await service.execute_job(chat_id, job1.id, provider=spy1, concurrency=4)
    assert res1.status == "paused_budget"
    completed_stage1 = res1.completed_chunks
    assert completed_stage1 == 4

    # Stage 2: Resume with cap=15 (enough to finish all 8 chunks + merge)
    job2 = service.create_or_resume_job(chat_id, max_calls=15, chunk_size=1, overlap=0)
    assert job2.id == job1.id  # Same job resumed!
    assert job2.completed_chunks == completed_stage1

    spy2 = ConcurrencySpyProvider(delay=0.03)
    res2 = await service.execute_job(chat_id, job2.id, provider=spy2, concurrency=4)

    assert res2.status == "completed"
    assert res2.completed_chunks == 8
    assert res2.processed_unique_messages == 8
    # spy2 should only have processed remaining 4 chunks, NOT all 8
    assert len(spy2.dispatched_chunks) == 4
