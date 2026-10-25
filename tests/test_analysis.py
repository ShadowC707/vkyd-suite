"""Offline tests for full-history analysis, chunk ledger, budget caps, and evidence validation."""

import asyncio
import json

import pytest

from app.analysis import AnalysisService
from app.db import Database
from app.providers.base import (
    ChunkAnalysisResult,
    MemeCandidate,
    MergeProfileResult,
    ProviderError,
    StyleAnalysis,
)
from app.providers.fake import FakeAIProvider


def _seed_chat_messages(db: Database, chat_id: str, count: int) -> None:
    """Helper to seed a test chat and N supported text messages."""
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
            "earliest_sent_at": "2024-01-01T10:00:00+00:00",
            "latest_sent_at": "2024-01-01T12:00:00+00:00",
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO chats (id, name, corpus_version, import_fingerprint,
                                          created_at, import_stats_json)
            VALUES (?, 'Analysis Test Chat', 1, ?, '2026-10-25T00:00:00Z', ?);
            """,
            (chat_id, f"fp_{chat_id}", json.dumps(stats)),
        )

        records = []
        for i in range(1, count + 1):
            records.append(
                (
                    f"m_{chat_id}_{i}",
                    chat_id,
                    i,
                    "2024-01-01T10:00:00+00:00",
                    "2024-01-01",
                    "message",
                    f"Message content number {i}",
                    None,
                    i,
                    1,
                )
            )

        conn.executemany(
            """
            INSERT OR REPLACE INTO messages (
                id, chat_id, source_message_id, sent_at, raw_date,
                kind, text, reply_to_source_id, source_order, is_supported_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """,
            records,
        )


@pytest.mark.anyio
async def test_multiple_chunks_honest_unique_message_coverage(tmp_path):
    """Analysis partitions corpus across chunks and counts each unique message exactly once."""
    db = Database(tmp_path / "analysis_test.db")
    db.run_migrations()

    chat_id = "chat_multi_chunk"
    total_msgs = 25
    _seed_chat_messages(db, chat_id, total_msgs)

    # 25 messages with chunk_size=10 and overlap=3 produces 3 chunks:
    # Chunk 0: msgs 1..10 (10 new)
    # Chunk 1: msgs 11..20 (10 new, 3 context)
    # Chunk 2: msgs 21..25 (5 new, 3 context)
    service = AnalysisService(db, chunk_size=10, overlap=3)
    provider = FakeAIProvider()

    estimate = service.estimate_chunks(chat_id)
    assert estimate.total_supported_messages == 25
    assert estimate.estimated_chunks == 3

    job = service.create_or_resume_job(chat_id)
    assert job.status == "pending"
    assert job.total_chunks == 3
    assert job.total_supported_messages == 25

    # Execute job
    completed_job = await service.execute_job(chat_id, job.id, provider)
    assert completed_job.status == "completed"
    assert completed_job.completed_chunks == 3
    # Exactly 25 unique messages covered; overlap does not inflate count
    assert completed_job.processed_unique_messages == 25
    assert completed_job.total_supported_messages == 25

    # Check profile and memes were persisted
    profile = service.get_latest_profile(chat_id)
    assert profile is not None
    assert profile.version == 1
    assert len(profile.memes) >= 1


@pytest.mark.anyio
async def test_call_budget_pause_and_resumption(tmp_path):
    """Job pauses at call cap with partial counts, and resumes without re-analyzing."""
    db = Database(tmp_path / "budget_test.db")
    db.run_migrations()

    chat_id = "chat_budget"
    _seed_chat_messages(db, chat_id, 30)

    # 30 messages with chunk_size=10 produces 3 chunks
    service = AnalysisService(db, chunk_size=10, overlap=2)
    provider = FakeAIProvider()

    # Cap at 2 calls: chunk 0 and chunk 1 succeed, then pauses before chunk 2
    job = service.create_or_resume_job(chat_id, max_calls=2)
    paused_job = await service.execute_job(chat_id, job.id, provider)

    assert paused_job.status == "paused_budget"
    assert paused_job.completed_chunks == 2
    assert paused_job.processed_unique_messages == 20
    assert paused_job.can_resume is True
    assert provider.call_count == 2

    # Verify chunk ledger state
    with db.connection() as conn:
        chunks = conn.execute(
            "SELECT ordinal, status FROM analysis_chunks WHERE job_id = ? ORDER BY ordinal;",
            (job.id,),
        ).fetchall()
        assert chunks[0]["status"] == "completed"
        assert chunks[1]["status"] == "completed"
        assert chunks[2]["status"] == "pending"

    # Now resume with higher call budget: only chunk 2 and merge will be called (2 calls)
    resumed_job_entry = service.create_or_resume_job(chat_id, max_calls=10)
    assert resumed_job_entry.status == "pending"

    final_job = await service.execute_job(chat_id, job.id, provider)
    assert final_job.status == "completed"
    assert final_job.completed_chunks == 3
    assert final_job.processed_unique_messages == 30
    # Provider was called for chunk 2 and merge only (+2 calls = 4 calls total)
    assert provider.call_count == 4


@pytest.mark.anyio
async def test_cancellation_preserves_completed_chunks(tmp_path):
    """Job cancellation leaves status paused_cancelled and preserves chunk progress."""
    db = Database(tmp_path / "cancel_test.db")
    db.run_migrations()

    chat_id = "chat_cancel"
    _seed_chat_messages(db, chat_id, 20)

    service = AnalysisService(db, chunk_size=10, overlap=0)
    job = service.create_or_resume_job(chat_id)

    cancelled = service.cancel_job(chat_id, job.id)
    assert cancelled.status == "paused_cancelled"
    assert cancelled.can_resume is True


def test_restart_recovery_recovers_stranded_jobs(tmp_path):
    """Stranded running jobs from a process crash are recovered to paused_cancelled."""
    db = Database(tmp_path / "restart_test.db")
    db.run_migrations()

    chat_id = "chat_restart"
    _seed_chat_messages(db, chat_id, 10)

    service = AnalysisService(db, chunk_size=5, overlap=0)
    job = service.create_or_resume_job(chat_id)

    # Simulate stranded running state
    with db.transaction() as conn:
        conn.execute("UPDATE analysis_jobs SET status = 'running' WHERE id = ?;", (job.id,))
        conn.execute(
            "UPDATE analysis_chunks SET status = 'running' WHERE job_id = ? AND ordinal = 0;",
            (job.id,),
        )

    # Recover
    service.recover_stale_jobs()

    recovered = service.get_job(chat_id, job.id)
    assert recovered.status == "paused_cancelled"
    assert recovered.last_error_code == "interrupted_restart"
    assert recovered.can_resume is True

    with db.connection() as conn:
        chunk0 = conn.execute(
            "SELECT status FROM analysis_chunks WHERE job_id = ? AND ordinal = 0;",
            (job.id,),
        ).fetchone()
        assert chunk0["status"] == "pending"


@pytest.mark.anyio
async def test_evidence_id_validation_filters_fabricated_and_cross_chat_ids(tmp_path):
    """Evidence IDs not present in the active chat's messages are safely stripped."""
    db = Database(tmp_path / "evidence_test.db")
    db.run_migrations()

    chat_id = "chat_active"
    _seed_chat_messages(db, chat_id, 5)  # valid source_message_ids: 1, 2, 3, 4, 5

    # Provider injects cross-chat and non-existent IDs: 1, 999 (fabricated), 8888 (cross-chat)
    provider = FakeAIProvider(fabricated_evidence_ids=[1, 999, 8888])
    service = AnalysisService(db, chunk_size=5, overlap=0)

    job = service.create_or_resume_job(chat_id)
    await service.execute_job(chat_id, job.id, provider)

    profile = service.get_latest_profile(chat_id)
    assert profile is not None
    assert len(profile.memes) >= 1

    meme = profile.memes[0]
    # Fabricated IDs 999 and 8888 were stripped; only authentic active-chat ID 1 remains!
    assert meme.evidence_message_ids == [1]


@pytest.mark.anyio
async def test_hierarchical_merge_handles_large_chunk_findings(tmp_path):
    """Hierarchical merge batches findings when > 10 chunks exist to bound context."""
    db = Database(tmp_path / "hierarchical_test.db")
    db.run_migrations()

    chat_id = "chat_large"
    # Seed 65 messages with chunk_size=5 -> 13 chunks (> 10 chunks)
    _seed_chat_messages(db, chat_id, 65)

    service = AnalysisService(db, chunk_size=5, overlap=1)
    provider = FakeAIProvider()

    job = service.create_or_resume_job(chat_id, max_calls=25)
    completed_job = await service.execute_job(chat_id, job.id, provider)

    assert completed_job.status == "completed"
    assert completed_job.completed_chunks == 13
    assert completed_job.processed_unique_messages == 65

    profile = service.get_latest_profile(chat_id)
    assert profile is not None
    assert profile.version == 1
    assert "Humor Bot Instructions" in profile.instructions_md


@pytest.mark.anyio
async def test_invalid_provider_output_sets_failed_status_and_preserves_progress(tmp_path):
    """Provider failure records last_error_code and preserves already completed chunks."""
    db = Database(tmp_path / "fail_test.db")
    db.run_migrations()

    chat_id = "chat_fail"
    _seed_chat_messages(db, chat_id, 20)  # 2 chunks with chunk_size=10

    # First chunk succeeds, second chunk fails
    service = AnalysisService(db, chunk_size=10, overlap=0)
    err = ProviderError("quota_exceeded", "Quota limit reached", retryable=True)
    provider = FakeAIProvider(should_fail=True, error_to_raise=err)

    job = service.create_or_resume_job(chat_id)
    with pytest.raises(ProviderError) as exc_info:
        await service.execute_job(chat_id, job.id, provider)

    assert exc_info.value.code == "quota_exceeded"

    failed_job = service.get_job(chat_id, job.id)
    assert failed_job.status == "failed"
    assert failed_job.last_error_code == "quota_exceeded"
    assert failed_job.can_resume is True


@pytest.mark.anyio
async def test_i1_exact_cap_13_chunks_cap_14_and_resumable_merge(tmp_path):
    """
    I1 probe reproduction:
    65 messages, chunk size 5, 13 chunks, max_calls=14.
    Actual provider invocations must not exceed 14, and persisted counter must agree.
    Resuming with higher budget finishes without re-paying for completed chunks or merge nodes.
    """
    db = Database(tmp_path / "i1_cap.db")
    db.run_migrations()

    chat_id = "chat_i1_cap"
    _seed_chat_messages(db, chat_id, 65)

    service = AnalysisService(db, chunk_size=5, overlap=1, merge_fan_in=5)
    provider = FakeAIProvider()

    # Verify estimation includes merge work (13 chunks + 3 intermediate + 1 root = 17 calls)
    estimate = service.estimate_chunks(chat_id)
    assert estimate.estimated_chunks == 13
    assert estimate.estimated_merge_calls == 4
    assert estimate.estimated_total_calls == 17

    # Start with max_calls=14: 13 chunks + 1 intermediate merge batch = 14 calls
    job = service.create_or_resume_job(chat_id, max_calls=14)
    paused = await service.execute_job(chat_id, job.id, provider)

    assert paused.status == "paused_budget"
    # Actual provider invocations must strictly equal 14; never 17!
    assert provider.call_count == 14
    assert paused.provider_calls == 14
    assert paused.completed_chunks == 13

    # Resuming with same or lower budget raises ValueError
    with pytest.raises(ValueError) as val_err:
        service.create_or_resume_job(chat_id, max_calls=14)
    assert "must be greater than current provider_calls" in str(val_err.value)

    # Resume with max_calls=20
    service.create_or_resume_job(chat_id, max_calls=20)
    completed = await service.execute_job(chat_id, job.id, provider)

    assert completed.status == "completed"
    # Total calls: 14 earlier + 2 remaining intermediate batches + 1 root = 17 total calls
    assert provider.call_count == 17
    assert completed.provider_calls == 17
    assert completed.completed_chunks == 13
    assert completed.processed_unique_messages == 65


@pytest.mark.anyio
async def test_i1_first_call_failure_records_attempt_in_ledger(tmp_path):
    """A failed first call must increment provider_calls in the durable ledger; never 0."""
    db = Database(tmp_path / "i1_fail_ledger.db")
    db.run_migrations()

    chat_id = "chat_i1_fail"
    _seed_chat_messages(db, chat_id, 10)

    service = AnalysisService(db, chunk_size=5, overlap=0)
    err = ProviderError("quota_exceeded", "Resource exhausted", retryable=False, status_code=429)
    provider = FakeAIProvider(should_fail=True, error_to_raise=err)

    job = service.create_or_resume_job(chat_id, max_calls=10)
    with pytest.raises(ProviderError):
        await service.execute_job(chat_id, job.id, provider)

    # In the durable ledger, provider_calls must be 1, NOT 0!
    failed_job = service.get_job(chat_id, job.id)
    assert failed_job.status == "failed"
    assert failed_job.provider_calls == 1
    assert provider.call_count == 1


@pytest.mark.anyio
async def test_i2_simultaneous_workers_single_owner_never_duplicates_coverage(tmp_path):
    """
    I2 probe reproduction:
    Run two execute_job coroutines for the same one-chunk/5-message job.
    Coverage must never exceed unique total (never 10/5), and chunks must not duplicate (never 2/1).
    """
    db = Database(tmp_path / "i2_concurrency.db")
    db.run_migrations()

    chat_id = "chat_i2_dup"
    _seed_chat_messages(db, chat_id, 5)

    service = AnalysisService(db, chunk_size=5, overlap=0)

    async def _slow_analyze():
        await asyncio.sleep(0.02)

    provider = FakeAIProvider(on_analyze_chunk=_slow_analyze)
    job = service.create_or_resume_job(chat_id, max_calls=10)

    # Launch two simultaneous execute_job calls
    res1, res2 = await asyncio.gather(
        service.execute_job(chat_id, job.id, provider),
        service.execute_job(chat_id, job.id, provider),
    )

    final_job = service.get_job(chat_id, job.id)
    assert final_job.total_supported_messages == 5
    assert final_job.processed_unique_messages == 5  # NEVER 10!
    assert final_job.total_chunks == 1
    assert final_job.completed_chunks == 1  # NEVER 2!
    assert final_job.status == "completed"


@pytest.mark.anyio
async def test_i2_cancellation_during_merge_is_sticky(tmp_path):
    """
    I2 probe reproduction:
    Cancellation requested while awaiting merge must remain sticky;
    must NOT persist profile or finish completed.
    """
    db = Database(tmp_path / "i2_cancel_merge.db")
    db.run_migrations()

    chat_id = "chat_i2_cancel"
    _seed_chat_messages(db, chat_id, 10)

    service = AnalysisService(db, chunk_size=5, overlap=0)

    async def _cancel_during_merge():
        service.cancel_job(chat_id, job.id)

    provider = FakeAIProvider(on_merge_findings=_cancel_during_merge)
    job = service.create_or_resume_job(chat_id, max_calls=10)

    res = await service.execute_job(chat_id, job.id, provider)
    assert res.status == "paused_cancelled"

    # Profile must not have been persisted
    profile = service.get_latest_profile(chat_id)
    assert profile is None

    # Check job in DB
    job_db = service.get_job(chat_id, job.id)
    assert job_db.status == "paused_cancelled"


@pytest.mark.anyio
async def test_i2_resume_while_old_worker_still_returning_does_not_overwrite(tmp_path):
    """When a job is cancelled and resumed, an old worker returning late must not overwrite."""
    db = Database(tmp_path / "i2_worker_superseded.db")
    db.run_migrations()

    chat_id = "chat_i2_worker"
    _seed_chat_messages(db, chat_id, 10)

    service = AnalysisService(db, chunk_size=5, overlap=0)

    w1_started = asyncio.Event()
    w1_can_proceed = asyncio.Event()

    async def _w1_hook():
        w1_started.set()
        await w1_can_proceed.wait()

    provider1 = FakeAIProvider(on_analyze_chunk=_w1_hook)
    provider2 = FakeAIProvider()

    job = service.create_or_resume_job(chat_id, max_calls=10)

    # Start worker 1 in background
    w1_task = asyncio.create_task(service.execute_job(chat_id, job.id, provider1))
    await w1_started.wait()

    # Cancel job while worker 1 is awaiting
    service.cancel_job(chat_id, job.id)

    # Resume job with worker 2
    service.create_or_resume_job(chat_id, max_calls=15)
    w2_task = asyncio.create_task(service.execute_job(chat_id, job.id, provider2))

    # Release worker 1
    w1_can_proceed.set()

    await asyncio.gather(w1_task, w2_task)

    final_job = service.get_job(chat_id, job.id)
    assert final_job.status == "completed"
    assert final_job.processed_unique_messages == 10
    assert final_job.completed_chunks == 2


@pytest.mark.anyio
async def test_i3_bounded_hierarchy_multiple_levels_with_spy(tmp_path):
    """
    I3 probe reproduction:
    Verify that tree reduction is recursive and strictly bounded.
    26 chunks with fan_in=5 produces: Level 1 (6 nodes) -> Level 2 (2 nodes) -> Level 3 (1 root).
    Every single merge call receives <= 5 findings; never unbounded.
    """
    db = Database(tmp_path / "i3_spy.db")
    db.run_migrations()

    chat_id = "chat_i3_hierarchy"
    # 130 messages / 5 per chunk = 26 chunks
    _seed_chat_messages(db, chat_id, 130)

    service = AnalysisService(db, chunk_size=5, overlap=0, merge_fan_in=5)
    # Spy provider will raise error if any merge call receives > 5 findings
    spy_provider = FakeAIProvider(max_findings_per_merge=5)

    job = service.create_or_resume_job(chat_id, max_calls=50)
    completed = await service.execute_job(chat_id, job.id, spy_provider)

    assert completed.status == "completed"
    assert completed.completed_chunks == 26
    assert completed.processed_unique_messages == 130

    # Verify that all recorded merge batches strictly adhered to <= 5
    assert len(spy_provider.recorded_merge_batch_sizes) >= 3
    for batch_size in spy_provider.recorded_merge_batch_sizes:
        assert batch_size <= 5


@pytest.mark.anyio
async def test_i3_oversized_message_split_and_honest_coverage(tmp_path):
    """An oversized message (e.g. 10k chars) is split into bounded slices and covered honestly."""
    db = Database(tmp_path / "i3_oversized.db")
    db.run_migrations()

    chat_id = "chat_i3_oversized"
    with db.transaction() as conn:
        stats = {
            "total_records": 1,
            "supported_text_messages": 1,
            "service_records": 0,
            "empty_messages": 0,
            "media_only_messages": 0,
            "unsupported_media_count": 0,
            "stickers_count": 0,
            "duplicate_records": 0,
            "earliest_sent_at": "2024-01-01T10:00:00+00:00",
            "latest_sent_at": "2024-01-01T10:00:00+00:00",
        }
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES (?, 'Oversized Chat', 1, 'fp_over', '2026-10-25', ?);
            """,
            (chat_id, json.dumps(stats)),
        )
        # 10,000 characters long message
        conn.execute(
            """
            INSERT INTO messages (
                id, chat_id, source_message_id, sent_at, raw_date,
                kind, text, reply_to_source_id, source_order, is_supported_text
            ) VALUES (
                'm_big', ?, 1, '2024-01-01T10:00:00Z', '2024-01-01',
                'message', ?, NULL, 1, 1
            );
            """,
            (chat_id, "ДОВГИЙ ТЕКСТ " * 800),
        )

    # Configure service with max_message_chars=2000 and max_chunk_chars=3000
    service = AnalysisService(
        db,
        chunk_size=5,
        max_message_chars=2000,
        max_chunk_chars=3000,
    )
    provider = FakeAIProvider(max_envelope_chars=3000)

    estimate = service.estimate_chunks(chat_id)
    assert estimate.total_supported_messages == 1
    # 10k chars / 2000 per slice = 5 slices -> grouped into bounded chunks
    assert estimate.estimated_chunks >= 2

    job = service.create_or_resume_job(chat_id, max_calls=10)
    completed = await service.execute_job(chat_id, job.id, provider)

    assert completed.status == "completed"
    # Exactly 1 unique message covered; never inflated by slices
    assert completed.total_supported_messages == 1
    assert completed.processed_unique_messages == 1


@pytest.mark.anyio
async def test_i4_owner_profile_preserved_as_active_when_analysis_runs(tmp_path):
    """
    I4 probe reproduction:
    Creating a valid owner profile before analysis must remain the active instructions;
    AI analysis results must become suggestions, not overwrite active owner edits.
    """
    db = Database(tmp_path / "i4_owner_preservation.db")
    db.run_migrations()

    chat_id = "chat_i4_owner"
    _seed_chat_messages(db, chat_id, 10)

    service = AnalysisService(db, chunk_size=5, overlap=0)

    # 1. Owner creates profile version 1
    owner_profile = service.update_profile(
        chat_id=chat_id,
        expected_version=0,
        instructions_md="# OWNER_EDIT_KEEP_THIS\nAlways be sarcastic.",
    )
    assert owner_profile.version == 1
    assert owner_profile.edited_by_user is True
    assert owner_profile.is_active is True
    assert "OWNER_EDIT_KEEP_THIS" in owner_profile.instructions_md

    # 2. Run analysis
    provider = FakeAIProvider()
    job = service.create_or_resume_job(chat_id, max_calls=10)
    await service.execute_job(chat_id, job.id, provider)

    # 3. Active profile must STILL be the owner's version 1!
    active_profile = service.get_latest_profile(chat_id)
    assert active_profile is not None
    assert active_profile.version == 1
    assert active_profile.edited_by_user is True
    assert active_profile.is_active is True
    assert "OWNER_EDIT_KEEP_THIS" in active_profile.instructions_md

    # 4. Check that AI analysis created version 2 as a suggestion (is_active=0)
    with db.connection() as conn:
        ai_row = conn.execute(
            "SELECT * FROM profiles WHERE chat_id = ? AND version = 2;", (chat_id,)
        ).fetchone()
        assert ai_row is not None
        assert ai_row["edited_by_user"] == 0
        assert ai_row["is_active"] == 0


@pytest.mark.anyio
async def test_i4_owner_edit_during_merge_await_survives(tmp_path):
    """If owner edits profile while merge is awaiting provider, the owner edit remains active."""
    db = Database(tmp_path / "i4_concurrent_edit.db")
    db.run_migrations()

    chat_id = "chat_i4_concurrent"
    _seed_chat_messages(db, chat_id, 10)

    service = AnalysisService(db, chunk_size=5, overlap=0)

    async def _owner_edits_during_merge():
        service.update_profile(
            chat_id=chat_id,
            expected_version=0,
            instructions_md="# CONCURRENT_OWNER_EDIT\nReply with puns.",
        )

    provider = FakeAIProvider(on_merge_findings=_owner_edits_during_merge)
    job = service.create_or_resume_job(chat_id, max_calls=10)
    await service.execute_job(chat_id, job.id, provider)

    active_profile = service.get_latest_profile(chat_id)
    assert active_profile is not None
    assert active_profile.version == 1
    assert active_profile.edited_by_user is True
    assert active_profile.is_active is True
    assert "CONCURRENT_OWNER_EDIT" in active_profile.instructions_md


@pytest.mark.anyio
async def test_r1_failed_chunk_resumption_completes_full_coverage(tmp_path):
    """
    R1 regression:
    10 messages, two chunks, first call fails.
    Resuming with a working provider must retry the failed chunk from its bounded payload,
    reach 10/10 messages, 2/2 chunks, and finish status=completed,
    never [failed, completed] at 5/10.
    """
    db = Database(tmp_path / "r1_resumption.db")
    db.run_migrations()

    chat_id = "chat_r1_resume"
    _seed_chat_messages(db, chat_id, 10)

    service = AnalysisService(db, chunk_size=5, overlap=0)

    # Provider fails on first call
    err = ProviderError("timeout", "Network timeout", retryable=True, status_code=504)
    failing_provider = FakeAIProvider(should_fail=True, error_to_raise=err)

    job = service.create_or_resume_job(chat_id, max_calls=10)
    with pytest.raises(ProviderError):
        await service.execute_job(chat_id, job.id, failing_provider)

    failed_state = service.get_job(chat_id, job.id)
    assert failed_state.status == "failed"
    assert failed_state.provider_calls == 1
    assert failed_state.completed_chunks == 0
    assert failed_state.processed_unique_messages == 0

    with db.connection() as conn:
        chunks = conn.execute(
            """
            SELECT ordinal, status, payload_json FROM analysis_chunks
            WHERE job_id = ? ORDER BY ordinal;
            """,
            (job.id,),
        ).fetchall()
        assert chunks[0]["status"] == "failed"
        assert chunks[0]["payload_json"] is not None
        assert chunks[1]["status"] == "pending"

    # Resume with working provider
    working_provider = FakeAIProvider()
    resumed_entry = service.create_or_resume_job(chat_id)
    assert resumed_entry.status == "pending"

    with db.connection() as conn:
        chunks_resumed = conn.execute(
            "SELECT ordinal, status FROM analysis_chunks WHERE job_id = ? ORDER BY ordinal;",
            (job.id,),
        ).fetchall()
        # Failed chunk must be reset to pending for retry
        assert chunks_resumed[0]["status"] == "pending"
        assert chunks_resumed[1]["status"] == "pending"

    completed = await service.execute_job(chat_id, job.id, working_provider)

    # Must be strictly 10/10 and 2/2 chunks completed! Never [failed, completed] at 5/10!
    assert completed.status == "completed"
    assert completed.completed_chunks == 2
    assert completed.total_chunks == 2
    assert completed.processed_unique_messages == 10
    assert completed.total_supported_messages == 10

    with db.connection() as conn:
        final_chunks = conn.execute(
            "SELECT ordinal, status FROM analysis_chunks WHERE job_id = ? ORDER BY ordinal;",
            (job.id,),
        ).fetchall()
        assert final_chunks[0]["status"] == "completed"
        assert final_chunks[1]["status"] == "completed"


@pytest.mark.anyio
async def test_r1_failed_split_message_fragment_resumption(tmp_path):
    """
    R1 regression:
    An oversized message split into slices whose first fragment call fails
    must resume from its original bounded payload in payload_json.
    """
    db = Database(tmp_path / "r1_split_fail.db")
    db.run_migrations()

    chat_id = "chat_r1_split"
    with db.transaction() as conn:
        stats = {
            "total_records": 1,
            "supported_text_messages": 1,
            "service_records": 0,
            "empty_messages": 0,
            "media_only_messages": 0,
            "unsupported_media_count": 0,
            "stickers_count": 0,
            "duplicate_records": 0,
            "earliest_sent_at": "2024-01-01T10:00:00Z",
            "latest_sent_at": "2024-01-01T10:00:00Z",
        }
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES (?, 'Split Chat', 1, 'fp_split', '2026-10-25', ?);
            """,
            (chat_id, json.dumps(stats)),
        )
        conn.execute(
            """
            INSERT INTO messages (id, chat_id, source_message_id, sent_at, raw_date,
                                  kind, text, source_order, is_supported_text)
            VALUES ('m_split', ?, 1, '2024-01-01T10:00:00Z', '2024-01-01', 'message', ?, 1, 1);
            """,
            (chat_id, "ДОВГИЙ ТЕКСТ ДЛЯ СПЛІТУ " * 100),
        )

    service = AnalysisService(db, chunk_size=1, max_message_chars=1000, max_chunk_chars=1500)

    # First call fails
    failing_provider = FakeAIProvider(should_fail=True)
    job = service.create_or_resume_job(chat_id, max_calls=20)
    with pytest.raises(ProviderError):
        await service.execute_job(chat_id, job.id, failing_provider)

    failed_job = service.get_job(chat_id, job.id)
    assert failed_job.status == "failed"

    # Resume with working provider: payload_json retains [Part 1/n] fragment
    working_provider = FakeAIProvider()
    service.create_or_resume_job(chat_id)
    completed = await service.execute_job(chat_id, job.id, working_provider)

    assert completed.status == "completed"
    assert completed.total_supported_messages == 1
    assert completed.processed_unique_messages == 1


@pytest.mark.anyio
async def test_r1_corrupted_findings_gate_rejects_completion(tmp_path):
    """
    R1 regression:
    If a completed chunk findings_json is corrupted/invalid, the merge gate must NOT
    suppress it and pretend completion; it must fail cleanly.
    """
    db = Database(tmp_path / "r1_corrupt.db")
    db.run_migrations()

    chat_id = "chat_r1_corrupt"
    _seed_chat_messages(db, chat_id, 10)

    service = AnalysisService(db, chunk_size=5, overlap=0)
    provider = FakeAIProvider()

    job = service.create_or_resume_job(chat_id, max_calls=10)

    # Simulate chunk 0 completed with invalid JSON
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE analysis_chunks
            SET status = 'completed', findings_json = 'INVALID_NON_JSON'
            WHERE job_id = ? AND ordinal = 0;
            """,
            (job.id,),
        )
        conn.execute(
            """
            UPDATE analysis_jobs
            SET completed_chunks = 1, processed_unique_messages = 5
            WHERE id = ?;
            """,
            (job.id,),
        )

    with pytest.raises(ProviderError) as exc_info:
        await service.execute_job(chat_id, job.id, provider)

    assert exc_info.value.code == "internal_error"
    # Verify job did NOT finish completed
    job_state = service.get_job(chat_id, job.id)
    assert job_state.status == "failed"


@pytest.mark.anyio
async def test_r2_obsolete_worker_isolation_with_events_success_and_exception(tmp_path):
    """
    R2 regression:
    Deterministic event ordering:
    old call held -> cancel/resume -> new call held on same chunk -> release old -> release new.
    Both old success and old exception must leave exactly 10/10 messages and 2/2 chunks!
    """
    db = Database(tmp_path / "r2_isolation.db")
    db.run_migrations()

    # --- Scenario A: Old worker returns SUCCESS after resume ---
    chat_id_a = "chat_r2_a"
    _seed_chat_messages(db, chat_id_a, 10)  # 2 chunks with chunk_size=5

    service_a = AnalysisService(db, chunk_size=5, overlap=0)

    w1_started = asyncio.Event()
    w1_can_proceed = asyncio.Event()
    w2_started = asyncio.Event()
    w2_can_proceed = asyncio.Event()

    async def _w1_hook():
        w1_started.set()
        await w1_can_proceed.wait()

    async def _w2_hook():
        w2_started.set()
        await w2_can_proceed.wait()

    p1 = FakeAIProvider(on_analyze_chunk=_w1_hook)
    p2 = FakeAIProvider(on_analyze_chunk=_w2_hook)

    job_a = service_a.create_or_resume_job(chat_id_a, max_calls=10)

    # 1. Start worker 1 on chunk 0
    t1 = asyncio.create_task(service_a.execute_job(chat_id_a, job_a.id, p1))
    await w1_started.wait()

    # 2. Cancel and resume
    service_a.cancel_job(chat_id_a, job_a.id)
    service_a.create_or_resume_job(chat_id_a, max_calls=15)

    # 3. Start worker 2 on chunk 0
    t2 = asyncio.create_task(service_a.execute_job(chat_id_a, job_a.id, p2))
    await w2_started.wait()

    # 4. Release old worker 1 (returns success)
    w1_can_proceed.set()
    await t1

    # 5. Release new worker 2
    w2_can_proceed.set()
    await t2

    final_a = service_a.get_job(chat_id_a, job_a.id)
    assert final_a.status == "completed"
    # Strictly 10/10 messages and 2/2 chunks! Never 15/10 or 3/2!
    assert final_a.processed_unique_messages == 10
    assert final_a.completed_chunks == 2
    assert final_a.total_chunks == 2

    # --- Scenario B: Old worker raises EXCEPTION after resume ---
    chat_id_b = "chat_r2_b"
    _seed_chat_messages(db, chat_id_b, 10)

    service_b = AnalysisService(db, chunk_size=5, overlap=0)

    w1b_started = asyncio.Event()
    w1b_can_proceed = asyncio.Event()
    w2b_started = asyncio.Event()
    w2b_can_proceed = asyncio.Event()

    async def _w1b_hook():
        w1b_started.set()
        await w1b_can_proceed.wait()

    async def _w2b_hook():
        w2b_started.set()
        await w2b_can_proceed.wait()

    err = ProviderError("quota_exceeded", "Old worker simulated error", retryable=False)
    p1b = FakeAIProvider(should_fail=True, error_to_raise=err, on_analyze_chunk=_w1b_hook)
    p2b = FakeAIProvider(on_analyze_chunk=_w2b_hook)

    job_b = service_b.create_or_resume_job(chat_id_b, max_calls=10)

    t1b = asyncio.create_task(service_b.execute_job(chat_id_b, job_b.id, p1b))
    await w1b_started.wait()

    service_b.cancel_job(chat_id_b, job_b.id)
    service_b.create_or_resume_job(chat_id_b, max_calls=15)

    t2b = asyncio.create_task(service_b.execute_job(chat_id_b, job_b.id, p2b))
    await w2b_started.wait()

    # Release worker 1b (raises error; must NOT mutate chunk owned by w2)
    w1b_can_proceed.set()
    with pytest.raises(ProviderError):
        await t1b

    # Release worker 2b
    w2b_can_proceed.set()
    await t2b

    final_b = service_b.get_job(chat_id_b, job_b.id)
    assert final_b.status == "completed"
    assert final_b.processed_unique_messages == 10
    assert final_b.completed_chunks == 2
    assert final_b.total_chunks == 2


@pytest.mark.anyio
async def test_r3_omitted_budget_preserves_cap_and_requires_explicit_increase(tmp_path):
    """
    R3 regression:
    A job paused at max_calls=1 must NOT implicitly expand to 50 when resumed without max_calls.
    Omitted budget preserves cap 1 and requires an explicit higher cap.
    """
    db = Database(tmp_path / "r3_budget.db")
    db.run_migrations()

    chat_id = "chat_r3_cap"
    _seed_chat_messages(db, chat_id, 20)  # 4 chunks

    service = AnalysisService(db, chunk_size=5, overlap=0)
    provider = FakeAIProvider()

    # Start with max_calls=1
    job = service.create_or_resume_job(chat_id, max_calls=1)
    paused = await service.execute_job(chat_id, job.id, provider)
    assert paused.status == "paused_budget"
    assert paused.provider_calls == 1
    assert paused.max_calls == 1

    # Resume with omitted max_calls: must raise ValueError and NOT expand to 50!
    with pytest.raises(ValueError) as exc_info:
        service.create_or_resume_job(chat_id, max_calls=None)
    assert "explicit higher max_calls is required" in str(exc_info.value)

    # Calling with same or lower budget also rejected
    with pytest.raises(ValueError):
        service.create_or_resume_job(chat_id, max_calls=1)

    # Resume with explicit higher cap (max_calls=10) succeeds
    resumed = service.create_or_resume_job(chat_id, max_calls=10)
    assert resumed.status == "pending"
    assert resumed.max_calls == 10

    completed = await service.execute_job(chat_id, job.id, provider)
    assert completed.status == "completed"


@pytest.mark.anyio
async def test_r4_intermediate_findings_accept_large_instructions_and_many_memes(tmp_path):
    """
    R4 regression:
    Intermediate merge findings with ~2,700 character instructions and 25 memes
    must not crash when converted to intermediate ChunkAnalysisResult,
    preserving style and resumability.
    """
    db = Database(tmp_path / "r4_intermediate.db")
    db.run_migrations()

    chat_id = "chat_r4_merge"
    # 35 messages with chunk_size=5 -> 7 chunks (> merge_fan_in=5, triggers intermediate round)
    _seed_chat_messages(db, chat_id, 35)

    service = AnalysisService(db, chunk_size=5, overlap=0, merge_fan_in=5)

    # Custom provider that returns 2,700 chars of instructions and 25 memes during merge
    large_instructions = "Правило спілкування в чаті: " + (
        "дуже довгий жарт " * 180
    )  # ~2,700 chars
    many_memes = [
        MemeCandidate(
            label=f"Meme #{i}",
            description=f"Description #{i}",
            evidence_source_message_ids=[1, 2],
        )
        for i in range(25)
    ]

    class LargeOutputMergeProvider(FakeAIProvider):
        async def merge_findings(self, chunk_findings, current_profile, chat_name):
            self.call_count += 1
            style = StyleAnalysis(
                languages=["Ukrainian"],
                reply_length="detailed",
                tone_and_register="academic humor",
                humor_techniques=["callbacks"],
                recurring_themes=["history"],
            )
            return MergeProfileResult(
                instructions_md=large_instructions,
                structured=style,
                memes=many_memes,
            )

    provider = LargeOutputMergeProvider()
    job = service.create_or_resume_job(chat_id, max_calls=20)
    completed = await service.execute_job(chat_id, job.id, provider)

    assert completed.status == "completed"
    assert completed.completed_chunks == 7
    assert completed.processed_unique_messages == 35

    # Check intermediate merge node persisted with full style intact
    with db.connection() as conn:
        node = conn.execute(
            """
            SELECT findings_json FROM analysis_merge_nodes
            WHERE job_id = ? AND level = 1 AND node_index = 0;
            """,
            (job.id,),
        ).fetchone()
        assert node is not None
        intermediate_parsed = ChunkAnalysisResult.model_validate_json(node["findings_json"])
        assert len(intermediate_parsed.instructions_summary) >= 2700
        assert len(intermediate_parsed.memes) == 25
        assert intermediate_parsed.style.tone_and_register == "academic humor"


@pytest.mark.anyio
async def test_r5_chunk_envelope_bounds_long_sender_metadata(tmp_path):
    """
    R5 regression:
    A message with a 5,000-character synthetic sender name is bounded so that
    total serialized prompt lines do not exceed the chunk envelope.
    """
    db = Database(tmp_path / "r5_envelope.db")
    db.run_migrations()

    chat_id = "chat_r5_sender"
    long_sender = "S" * 5000
    with db.transaction() as conn:
        stats = {
            "total_records": 1,
            "supported_text_messages": 1,
            "service_records": 0,
            "empty_messages": 0,
            "media_only_messages": 0,
            "unsupported_media_count": 0,
            "stickers_count": 0,
            "duplicate_records": 0,
            "earliest_sent_at": "2024-01-01T10:00:00Z",
            "latest_sent_at": "2024-01-01T10:00:00Z",
        }
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES (?, 'Long Sender Chat', 1, 'fp_sender', '2026-10-25', ?);
            """,
            (chat_id, json.dumps(stats)),
        )
        conn.execute(
            """
            INSERT INTO messages (id, chat_id, source_message_id, sender_name, sent_at,
                                  raw_date, kind, text, source_order, is_supported_text)
            VALUES ('m_long_sender', ?, 1, ?, '2024-01-01T10:00:00Z', '2024-01-01',
                    'message', 'Hi', 1, 1);
            """,
            (chat_id, long_sender),
        )

    service = AnalysisService(db, chunk_size=5, max_chunk_chars=25000)

    # Provider enforces max_envelope_chars=25000
    provider = FakeAIProvider(max_envelope_chars=25000)

    job = service.create_or_resume_job(chat_id, max_calls=5)
    completed = await service.execute_job(chat_id, job.id, provider)

    assert completed.status == "completed"
    assert completed.processed_unique_messages == 1


@pytest.mark.anyio
async def test_r5_outer_service_exception_never_leaks_input_dump(tmp_path):
    """
    R5 regression:
    Outer AnalysisService exception handler must raise fixed safe ProviderError
    and NEVER dump input_value or raw model responses.
    """
    db = Database(tmp_path / "r5_safe_err.db")
    db.run_migrations()

    chat_id = "chat_r5_safe"
    _seed_chat_messages(db, chat_id, 5)

    service = AnalysisService(db, chunk_size=5, overlap=0)

    secret_string = "SUPER_SECRET_PAYLOAD_PRIVATE_CHAT_TEXT_12345"

    class ExplodingProvider(FakeAIProvider):
        async def analyze_chunk(self, messages, chat_name, max_output_tokens=None):
            # Raise an exception containing secret string
            raise RuntimeError(f"Unexpected internal failure with {secret_string}")

    provider = ExplodingProvider()
    job = service.create_or_resume_job(chat_id, max_calls=5)

    with pytest.raises(ProviderError) as exc_info:
        await service.execute_job(chat_id, job.id, provider)

    err = exc_info.value
    assert err.code == "internal_error"
    # Must be a fixed safe message! Secret string must NEVER be present!
    assert secret_string not in err.message
    assert "input_value" not in err.message
    assert err.message == "Analysis execution encountered an unexpected internal error."


@pytest.mark.anyio
async def test_g1_legacy_payload_recovery_honest_coverage(tmp_path):
    """
    G1 regression:
    An older legacy chunk holding five messages of 8,000 characters each (40,000 chars total)
    with no payload_json must be durably reconciled into bounded fragments covering ALL 5
    messages, never silently taking a prefix (IDs 1, 2) and crediting 5/5.
    """
    db = Database(tmp_path / "g1_legacy_coverage.db")
    db.run_migrations()

    chat_id = "chat_g1_legacy"
    with db.transaction() as conn:
        stats = {
            "total_records": 5,
            "supported_text_messages": 5,
            "service_records": 0,
            "empty_messages": 0,
            "media_only_messages": 0,
            "unsupported_media_count": 0,
            "stickers_count": 0,
            "duplicate_records": 0,
            "earliest_sent_at": "2024-01-01T10:00:00Z",
            "latest_sent_at": "2024-01-01T10:04:00Z",
        }
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES (?, 'Legacy G1 Chat', 1, 'fp_g1', '2026-10-25', ?);
            """,
            (chat_id, json.dumps(stats)),
        )

        for src_id in [1, 2, 3, 4, 5]:
            msg_text = f"Message {src_id}: " + ("x" * 7980)
            conn.execute(
                """
                INSERT INTO messages (id, chat_id, source_message_id, sender_name, sent_at,
                                      raw_date, kind, text, source_order, is_supported_text)
                VALUES (?, ?, ?, 'Alice', '2024-01-01T10:00:00Z', '2024-01-01',
                        'message', ?, ?, 1);
                """,
                (f"msg_{src_id}", chat_id, src_id, msg_text, src_id),
            )

        # Insert a legacy count-based chunk holding all 5 messages, no payload_json
        job_id = "job_g1_legacy"
        conn.execute(
            """
            INSERT INTO analysis_jobs (
                id, chat_id, corpus_version, status,
                total_supported_messages, processed_unique_messages,
                total_chunks, completed_chunks, provider_calls,
                max_calls, last_error_code, active_worker_id, created_at, updated_at
            ) VALUES (?, ?, 1, 'pending', 5, 0, 1, 0, 0, 20, NULL, NULL,
                     '2026-10-25', '2026-10-25');
            """,
            (job_id, chat_id),
        )
        conn.execute(
            """
            INSERT INTO analysis_chunks (
                id, job_id, ordinal, source_ids_json, context_ids_json,
                status, findings_json, payload_json, attempts, claimed_worker_id
            ) VALUES ('chunk_legacy', ?, 0, '[1, 2, 3, 4, 5]', '[]',
                     'pending', NULL, NULL, 0, NULL);
            """,
            (job_id,),
        )

    seen_analyzed_ids: list[list[int]] = []
    seen_payload_lengths: list[int] = []

    class G1SpyProvider(FakeAIProvider):
        async def analyze_chunk(self, messages, chat_name, max_output_tokens=None):
            ids = [m["source_message_id"] for m in messages]
            seen_analyzed_ids.append(ids)
            total_chars = sum(len(m.get("text", "")) for m in messages)
            seen_payload_lengths.append(total_chars)
            return await super().analyze_chunk(messages, chat_name, max_output_tokens)

    service = AnalysisService(db, chunk_size=5, max_chunk_chars=25000)
    provider = G1SpyProvider()

    completed = await service.execute_job(chat_id, job_id, provider)

    assert completed.status == "completed"
    assert completed.processed_unique_messages == 5
    assert completed.total_supported_messages == 5

    # Verify provider saw ALL source IDs across all bounded fragments!
    all_seen_ids = {src_id for chunk_ids in seen_analyzed_ids for src_id in chunk_ids}
    assert all_seen_ids == {1, 2, 3, 4, 5}

    # Verify provider saw the complete payload (approx 40k chars), not just a 15k prefix!
    total_analyzed_chars = sum(seen_payload_lengths)
    assert total_analyzed_chars >= 39000


@pytest.mark.anyio
async def test_g1_unreconstructable_legacy_job_fails_safely_before_dispatch(tmp_path):
    """
    G1 regression:
    An unfinished legacy chunk whose source messages cannot be found in the database
    must fail cleanly before dispatch with last_error_code='recovery_required'
    and can_resume=False, never dispatching calls or wasting budget.
    """
    db = Database(tmp_path / "g1_unrec.db")
    db.run_migrations()

    chat_id = "chat_g1_unrec"
    with db.transaction() as conn:
        stats = {
            "total_records": 0,
            "supported_text_messages": 0,
            "service_records": 0,
            "empty_messages": 0,
            "media_only_messages": 0,
            "unsupported_media_count": 0,
            "stickers_count": 0,
            "duplicate_records": 0,
            "earliest_sent_at": None,
            "latest_sent_at": None,
        }
        conn.execute(
            """
            INSERT INTO chats (id, name, corpus_version, import_fingerprint,
                              created_at, import_stats_json)
            VALUES (?, 'Unreconstructable Chat', 1, 'fp_unrec', '2026-10-25', ?);
            """,
            (chat_id, json.dumps(stats)),
        )

        job_id = "job_g1_unrec"
        conn.execute(
            """
            INSERT INTO analysis_jobs (
                id, chat_id, corpus_version, status,
                total_supported_messages, processed_unique_messages,
                total_chunks, completed_chunks, provider_calls,
                max_calls, last_error_code, active_worker_id, created_at, updated_at
            ) VALUES (?, ?, 1, 'pending', 2, 0, 1, 0, 0, 10, NULL, NULL,
                     '2026-10-25', '2026-10-25');
            """,
            (job_id, chat_id),
        )
        conn.execute(
            """
            INSERT INTO analysis_chunks (
                id, job_id, ordinal, source_ids_json, context_ids_json,
                status, findings_json, payload_json, attempts, claimed_worker_id
            ) VALUES ('chunk_unrec', ?, 0, '[991, 992]', '[]', 'pending', NULL, NULL, 0, NULL);
            """,
            (job_id,),
        )

    class ZeroDispatchSpy(FakeAIProvider):
        def __init__(self):
            super().__init__()
            self.dispatched = 0

        async def analyze_chunk(self, messages, chat_name, max_output_tokens=None):
            self.dispatched += 1
            return await super().analyze_chunk(messages, chat_name, max_output_tokens)

    service = AnalysisService(db)
    provider = ZeroDispatchSpy()

    with pytest.raises(ProviderError) as exc_info:
        await service.execute_job(chat_id, job_id, provider)

    assert exc_info.value.code == "recovery_required"
    assert provider.dispatched == 0

    job_state = service.get_job(chat_id, job_id)
    assert job_state.status == "failed"
    assert job_state.last_error_code == "recovery_required"
    assert job_state.can_resume is False
    assert job_state.provider_calls == 0

    # Resuming an unrecoverable job must be rejected with ValueError
    with pytest.raises(ValueError, match="recovery"):
        service.create_or_resume_job(chat_id)


@pytest.mark.anyio
async def test_g1_malformed_payload_reconstruction_preserves_paid_work(tmp_path):
    """
    G1 regression:
    An unfinished chunk with malformed payload_json is reconciled from database messages,
    while completed paid chunks and call accounting are 100% preserved.
    """
    db = Database(tmp_path / "g1_malformed_preserve.db")
    db.run_migrations()

    chat_id = "chat_g1_malformed"
    _seed_chat_messages(db, chat_id, 4)

    service = AnalysisService(db, chunk_size=2, overlap=0)
    provider = FakeAIProvider()

    job = service.create_or_resume_job(chat_id, max_calls=10)

    # Complete first chunk normally
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE analysis_chunks
            SET status = 'completed', findings_json = ?
            WHERE job_id = ? AND ordinal = 0;
            """,
            (
                ChunkAnalysisResult(
                    style=StyleAnalysis(), memes=[], instructions_summary="Summary 0"
                ).model_dump_json(),
                job.id,
            ),
        )
        # Corrupt chunk 1's payload
        conn.execute(
            """
            UPDATE analysis_chunks
            SET status = 'pending', payload_json = 'INVALID_JSON_CORRUPTED'
            WHERE job_id = ? AND ordinal = 1;
            """,
            (job.id,),
        )
        conn.execute(
            """
            UPDATE analysis_jobs
            SET completed_chunks = 1, processed_unique_messages = 2, provider_calls = 1
            WHERE id = ?;
            """,
            (job.id,),
        )

    completed = await service.execute_job(chat_id, job.id, provider)

    assert completed.status == "completed"
    assert completed.completed_chunks == 2
    assert completed.total_chunks == 2
    assert completed.processed_unique_messages == 4
    assert completed.total_supported_messages == 4


@pytest.mark.anyio
async def test_g2_exact_five_by_9000_merge_adapts_to_serialized_envelope_with_spy(tmp_path):
    """
    G2 regression:
    Five schema-valid 9,000-character chunk summaries must adapt to the serialized envelope,
    splitting into bounded intermediate batches without exceeding GeminiProvider's 40k guard.
    Exercises pause at budget boundary and resume reusing completed nodes without repeating calls.
    """
    db = Database(tmp_path / "g2_five_by_9000.db")
    db.run_migrations()

    chat_id = "chat_g2_five"
    _seed_chat_messages(db, chat_id, 5)

    with db.transaction() as conn:
        job_id = "job_g2_five"
        conn.execute(
            """
            INSERT INTO analysis_jobs (
                id, chat_id, corpus_version, status,
                total_supported_messages, processed_unique_messages,
                total_chunks, completed_chunks, provider_calls,
                max_calls, last_error_code, active_worker_id, created_at, updated_at
            ) VALUES (?, ?, 1, 'pending', 5, 5, 5, 5, 5, 6, NULL, NULL, '2026-10-25', '2026-10-25');
            """,
            (job_id, chat_id),
        )

        for ordinal in range(5):
            chunk_res = ChunkAnalysisResult(
                style=StyleAnalysis(),
                memes=[],
                instructions_summary="K" * 9000,  # 9,000 characters schema-valid summary
            )
            conn.execute(
                """
                INSERT INTO analysis_chunks (
                    id, job_id, ordinal, source_ids_json, context_ids_json,
                    status, findings_json, payload_json, attempts, claimed_worker_id
                ) VALUES (?, ?, ?, ?, '[]', 'completed', ?, '[]', 1, NULL);
                """,
                (
                    f"c_g2_{ordinal}",
                    job_id,
                    ordinal,
                    json.dumps([ordinal + 1]),
                    chunk_res.model_dump_json(),
                ),
            )

    # Test through real production GeminiProvider logic with prompt envelope verification
    from app.providers.gemini import GeminiProvider

    class ProductionGeminiSpy(GeminiProvider):
        def __init__(self):
            super().__init__(api_key="test-key-offline")
            self.dispatched_prompts: list[str] = []
            self.dispatched_calls = 0

        async def _call_structured(
            self, prompt, response_schema, system_instruction=None, max_output_tokens=None
        ):
            # Strict assertion: prompt must NEVER exceed the 40k adapter envelope
            assert len(prompt) <= 40000, f"Envelope guard violated: {len(prompt)} > 40000 chars"
            self.dispatched_prompts.append(prompt)
            self.dispatched_calls += 1
            if response_schema is MergeProfileResult:
                return MergeProfileResult(
                    instructions_md="Synthesized instructions from intermediate/root merge.",
                    structured=StyleAnalysis(),
                    memes=[],
                )
            return ChunkAnalysisResult()

    service = AnalysisService(db, merge_fan_in=5)
    spy = ProductionGeminiSpy()

    # Step 1: Run with max_calls=6.
    # 5 calls are already in ledger. Only 1 call is permitted before budget pause.
    job_step1 = await service.execute_job(chat_id, job_id, spy)

    assert job_step1.status == "paused_budget"
    assert job_step1.provider_calls == 6
    assert spy.dispatched_calls == 1  # Exactly 1 intermediate node dispatched
    assert len(spy.dispatched_prompts) == 1
    assert len(spy.dispatched_prompts[0]) <= 40000

    # Verify that Node 0 was persisted in analysis_merge_nodes
    with db.connection() as conn:
        node0 = conn.execute(
            """
            SELECT level, node_index, status FROM analysis_merge_nodes
            WHERE job_id = ? AND level = 1 AND node_index = 0;
            """,
            (job_id,),
        ).fetchone()
        assert node0 is not None
        assert node0["status"] == "completed"

    # Step 2: Resume with max_calls=8
    service.create_or_resume_job(chat_id, max_calls=8)
    completed_step2 = await service.execute_job(chat_id, job_id, spy)

    assert completed_step2.status == "completed"
    assert completed_step2.provider_calls == 7
    # Total calls across both runs: 1 intermediate (Node 0) in step 1 + 1 root merge in step 2 = 2
    # Node 0 was reused from analysis_merge_nodes and never repeated!
    assert spy.dispatched_calls == 2
    assert len(spy.dispatched_prompts) == 2
    for p in spy.dispatched_prompts:
        assert len(p) <= 40000

    # Verify profile was successfully created
    profile = service.get_latest_profile(chat_id)
    assert profile is not None
    assert "Synthesized instructions" in profile.instructions_md


@pytest.mark.anyio
async def test_g2_individually_oversized_node_handled_explicitly_without_dispatch_budget(tmp_path):
    """
    G2 / S5 regression:
    A truly schema-valid oversized serialized envelope node (>40,000 chars)
    is rejected before call reservation.
    Verifies no local budget charge, no provider dispatch, no silent mutation,
    and unrecoverable state with can_resume=False.
    """
    db = Database(tmp_path / "g2_oversized_local.db")
    db.run_migrations()

    chat_id = "chat_g2_oversized"
    _seed_chat_messages(db, chat_id, 2)

    # 40 valid MemeCandidates with lengths strictly respecting field max_lengths:
    # label <= 150, description <= 500, usage_context <= 500
    memes = [
        MemeCandidate(
            label=f"Meme {i} " + ("title " * 15)[:130],
            description=("Detailed explanation of humor context and usage inside the group " * 10)[
                :490
            ],
            usage_context=("Used specifically during morning discussions and coffee chats " * 10)[
                :490
            ],
            evidence_source_message_ids=[1],
            confidence=0.95,
        )
        for i in range(40)
    ]
    # Plus 10,000 characters of instructions_summary = >46,000 total characters
    oversized_summary = "A" * 10000
    oversized_finding = ChunkAnalysisResult(
        style=StyleAnalysis(),
        memes=memes,
        instructions_summary=oversized_summary,
    )
    oversized_json = oversized_finding.model_dump_json()
    assert len(oversized_json) > 40000  # Schema-valid and strictly oversized envelope!

    with db.transaction() as conn:
        job_id = "job_g2_oversized"
        conn.execute(
            """
            INSERT INTO analysis_jobs (
                id, chat_id, corpus_version, status,
                total_supported_messages, processed_unique_messages,
                total_chunks, completed_chunks, provider_calls,
                max_calls, last_error_code, active_worker_id, created_at, updated_at
            ) VALUES (?, ?, 1, 'pending', 2, 2, 2, 2, 0, 5, NULL, NULL, '2026-10-25', '2026-10-25');
            """,
            (job_id, chat_id),
        )

        # Chunk 0 has the oversized finding
        conn.execute(
            """
            INSERT INTO analysis_chunks (
                id, job_id, ordinal, source_ids_json, context_ids_json,
                status, findings_json, payload_json, attempts, claimed_worker_id
            ) VALUES (?, ?, 0, '[1]', '[]', 'completed', ?, '[]', 1, NULL);
            """,
            ("c_over_0", job_id, oversized_json),
        )
        # Chunk 1 has a standard finding
        conn.execute(
            """
            INSERT INTO analysis_chunks (
                id, job_id, ordinal, source_ids_json, context_ids_json,
                status, findings_json, payload_json, attempts, claimed_worker_id
            ) VALUES (?, ?, 1, '[2]', '[]', 'completed', ?, '[]', 1, NULL);
            """,
            (
                "c_over_1",
                job_id,
                ChunkAnalysisResult(
                    style=StyleAnalysis(),
                    memes=[],
                    instructions_summary="Short summary",
                ).model_dump_json(),
            ),
        )

    service = AnalysisService(db)
    provider = FakeAIProvider()

    with pytest.raises(ProviderError) as exc_info:
        await service.execute_job(chat_id, job_id, provider)

    assert exc_info.value.code == "envelope_exceeded"

    # Verify job state in DB:
    job = service.get_job(chat_id, job_id)
    assert job.status == "failed"
    assert job.last_error_code == "envelope_exceeded"
    assert job.can_resume is False
    # Budget was NOT charged locally, and provider was NOT called
    assert job.provider_calls == 0
    assert provider.call_count == 0

    # Verify findings in database were NOT mutated or sliced
    with db.connection() as conn:
        row = conn.execute(
            "SELECT findings_json FROM analysis_chunks WHERE id = 'c_over_0';"
        ).fetchone()
        loaded = ChunkAnalysisResult.model_validate_json(row["findings_json"])
        assert len(loaded.instructions_summary) == 10000
        assert len(loaded.memes) == 40

    # Verify attempting to resume this unrecoverable envelope error fails with clear ValueError
    with pytest.raises(ValueError, match="unrecoverable error"):
        service.create_or_resume_job(chat_id, max_calls=10)


@pytest.mark.anyio
async def test_s5_missing_payload_item_rejected_by_validator_and_reconstructed(tmp_path):
    """
    S5 regression:
    A valid JSON payload containing just 1 of 5 expected source messages is rejected by
    _validate_chunk_payload instead of completing at 5/5 after sending only ID 1.
    Reconciliation restores full coverage durably before dispatch.
    """
    db = Database(tmp_path / "s5_missing_payload.db")
    db.run_migrations()

    chat_id = "chat_s5_missing"
    _seed_chat_messages(db, chat_id, 5)

    service = AnalysisService(db)

    # Chunk expected to cover IDs 1, 2, 3, 4, 5
    corrupt_payload = [
        {
            "source_message_id": 1,
            "sender_name": "Author",
            "sent_at": "2024-01-01T10:00:00Z",
            "text": "Message 1 text",
            "is_primary": True,
        }
    ]
    chunk_row = {
        "job_id": "dummy_job",
        "source_ids_json": json.dumps([1, 2, 3, 4, 5]),
        "context_ids_json": json.dumps([]),
        "payload_json": json.dumps(corrupt_payload),
    }

    # Validator MUST return None because IDs 2, 3, 4, 5 are missing!
    val_res = service._validate_chunk_payload(chunk_row, chat_id=chat_id)
    assert val_res is None

    # Foreign chat ID in payload must also be rejected
    foreign_payload = [
        {
            "source_message_id": 9999,  # does not exist in this chat
            "sender_name": "Attacker",
            "sent_at": "2024-01-01T10:00:00Z",
            "text": "Foreign text",
            "is_primary": True,
        }
    ]
    chunk_foreign = {
        "job_id": "dummy_job",
        "source_ids_json": json.dumps([1]),
        "context_ids_json": json.dumps([]),
        "payload_json": json.dumps(foreign_payload),
    }
    assert service._validate_chunk_payload(chunk_foreign, chat_id=chat_id) is None


def test_s5_no_silent_mutation_in_merge_partitioning():
    """
    S5 regression:
    _partition_merge_batches must NOT silently mutate a 10,000-character summary
    or slice memes down to manufacture fit.
    """
    db = Database(":memory:")
    db.run_migrations()
    service = AnalysisService(db)

    memes = [
        MemeCandidate(
            label=f"Meme {i}",
            description="Description",
            usage_context="Context",
            evidence_source_message_ids=[1],
        )
        for i in range(35)
    ]
    summary_10k = "M" * 10000
    item = ChunkAnalysisResult(
        style=StyleAnalysis(),
        memes=memes,
        instructions_summary=summary_10k,
    )

    # Partitioning must preserve the item contents completely
    # (or raise ProviderError("envelope_exceeded") if individually oversized)
    try:
        batches = service._partition_merge_batches([item], "Test Chat", max_prompt_chars=38000)
        assert len(batches) == 1
        assert len(item.instructions_summary) == 10000
        assert len(item.memes) == 35
    except ProviderError as pe:
        assert pe.code == "envelope_exceeded"
        assert len(item.instructions_summary) == 10000
        assert len(item.memes) == 35


def test_profile_suggestion_inspect_and_apply_flow(tmp_path):
    """
    Verify profile suggestion inspect and apply flow:
    - AI analysis while owner profile is active creates an inactive suggestion.
    - get_suggested_profile retrieves the suggestion.
    - apply_suggested_profile enforces version conflict check on expected_active_version.
    - Successful apply makes the suggestion the new active profile with edited_by_user=1.
    """
    db = Database(tmp_path / "prof_sugg.db")
    db.run_migrations()

    chat_id = "chat_sugg"
    _seed_chat_messages(db, chat_id, 3)

    service = AnalysisService(db)

    # 1. Owner creates active profile v1
    v1 = service.update_profile(
        chat_id=chat_id,
        expected_version=0,
        instructions_md="Owner instructions v1",
        memes_update=[],
    )
    assert v1.version == 1
    assert v1.is_active is True
    assert v1.edited_by_user is True

    # 2. Add an AI suggestion v2 (is_active=0, edited_by_user=0)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO profiles (
                id, chat_id, version, source_job_id,
                instructions_md, structured_json, edited_by_user, is_active, created_at
            ) VALUES ('prof_sugg_v2', ?, 2, 'job_ai', 'AI Suggested Instructions v2',
                     '{}', 0, 0, '2026-10-25');
            """,
            (chat_id,),
        )
        conn.execute(
            """
            INSERT INTO memes (
                id, chat_id, profile_id, label, description, usage_context,
                evidence_message_ids, confidence, approval_state
            ) VALUES ('m_sugg', ?, 'prof_sugg_v2', 'Suggested Meme', 'AI found this',
                     '', '[]', 0.9, 'unverified');
            """,
            (chat_id,),
        )

    # 3. get_suggested_profile retrieves v2
    sugg = service.get_suggested_profile(chat_id)
    assert sugg is not None
    assert sugg.version == 2
    assert sugg.instructions_md == "AI Suggested Instructions v2"
    assert len(sugg.memes) == 1
    assert sugg.memes[0].label == "Suggested Meme"

    # 4. Version conflict on apply with wrong expected_active_version
    with pytest.raises(ValueError, match="Version conflict"):
        service.apply_suggested_profile(chat_id=chat_id, expected_active_version=99)

    # 5. Successful apply with expected_active_version=1
    applied = service.apply_suggested_profile(chat_id=chat_id, expected_active_version=1)
    assert applied.version == 3  # new active version allocated
    assert applied.is_active is True
    assert applied.edited_by_user is True
    assert applied.instructions_md == "AI Suggested Instructions v2"
    assert len(applied.memes) == 1
    assert applied.memes[0].id != "m_sugg"  # fresh snapshot UUID
    assert applied.memes[0].label == "Suggested Meme"

    # 6. Once applied, no pending suggestion remains
    assert service.get_suggested_profile(chat_id) is None


@pytest.mark.anyio
async def test_merge_compaction_of_unpairable_25k_nodes_and_envelope_resume(tmp_path):
    """
    Verify merge compaction and envelope resume:
    1. An intermediate node > 18k serialized prompt chars is compacted by
       _compact_oversized_intermediate to <= MAX_MERGE_NODE_PROMPT_CHARS (18,000).
    2. A job with completed chunks and merge nodes that failed with envelope_exceeded is marked
       can_resume=True and can be resumed via create_or_resume_job.
    """
    from app.providers.base import MemeCandidate

    db = Database(tmp_path / "compaction_resume.db")
    db.run_migrations()

    chat_id = "chat_compaction_test"
    _seed_chat_messages(db, chat_id, 20)
    service = AnalysisService(db)

    # 1. Test compaction
    large_memes = [
        MemeCandidate(
            label=f"Meme {i}",
            description="D" * 400,
            usage_context="U" * 400,
            evidence_source_message_ids=[i, i + 1, i + 2],
            confidence=0.9,
        )
        for i in range(30)
    ]
    oversized_node = ChunkAnalysisResult(
        style=StyleAnalysis(),
        memes=large_memes,
        instructions_summary="S" * 4500,
    )
    oversized_prompt_len = service._calculate_merge_prompt_length(
        [oversized_node], None, "Test Chat"
    )
    assert oversized_prompt_len > 18000

    compacted = service._compact_oversized_intermediate(oversized_node, "Test Chat")
    compacted_prompt_len = service._calculate_merge_prompt_length([compacted], None, "Test Chat")
    assert compacted_prompt_len <= 18000
    assert len(compacted.memes) > 0
    assert any(len(m.evidence_source_message_ids) > 0 for m in compacted.memes)

    # 2. Test resume of recoverable envelope error
    job = service.create_or_resume_job(chat_id, max_calls=10)
    # Simulate completed chunk and an existing merge node, then fail with envelope_exceeded
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE analysis_chunks
            SET status = 'completed', findings_json = ?
            WHERE job_id = ?;
            """,
            (compacted.model_dump_json(), job.id),
        )
        conn.execute(
            """
            INSERT INTO analysis_merge_nodes (
                id, job_id, level, node_index, status, findings_json, created_at
            ) VALUES ('node_1', ?, 1, 0, 'completed', ?, '2026-10-25');
            """,
            (job.id, compacted.model_dump_json()),
        )
        conn.execute(
            """
            UPDATE analysis_jobs
            SET status = 'failed',
                last_error_code = 'envelope_exceeded',
                completed_chunks = total_chunks
            WHERE id = ?;
            """,
            (job.id,),
        )

    # Inspect job: can_resume must be True
    job_view = service.get_job(chat_id, job.id)
    assert job_view.status == "failed"
    assert job_view.last_error_code == "envelope_exceeded"
    assert job_view.can_resume is True

    # Resuming the job should reset it to pending without error
    resumed = service.create_or_resume_job(chat_id)
    assert resumed.id == job.id
    assert resumed.status == "pending"
    assert resumed.last_error_code is None

