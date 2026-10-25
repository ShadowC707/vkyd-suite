"""Archive analysis runner for Task 04 full-history Gemini run.

Strictly operates on aggregate metadata. Never prints private message text,
sender names, raw export contents, or private generated text to stdout or reports.
"""

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

# Ensure project root is on sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analysis import AnalysisService  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import get_db  # noqa: E402
from app.importer import TelegramImporter  # noqa: E402
from app.models import ReplyRequest  # noqa: E402
from app.providers.gemini import GeminiProvider  # noqa: E402
from app.replies import ReplyService  # noqa: E402
from app.retrieval import MemoryRetriever  # noqa: E402

ARCHIVE_PATH = Path("tests/fixtures/result.json")
EXPECTED_TOTAL_RECORDS = 261703
EXPECTED_SUPPORTED_RECORDS = 241569
INPUT_TOKEN_PRICE_PER_M = 0.75
OUTPUT_TOKEN_PRICE_PER_M = 3.75


def run_import(db, archive_path: Path):
    """Import the archive locally and idempotently without resetting other chats."""
    ts = datetime.now(UTC).isoformat()
    print(f"[{ts}] Checking archive file: {archive_path}")
    if not archive_path.exists():
        raise FileNotFoundError(f"Archive file not found: {archive_path}")

    file_size = archive_path.stat().st_size
    print(f"[{datetime.now(UTC).isoformat()}] Archive file size: {file_size:,} bytes")

    # Read archive bytes
    t0 = time.time()
    raw_bytes = archive_path.read_bytes()
    read_dur = time.time() - t0
    print(f"[{datetime.now(UTC).isoformat()}] Read {len(raw_bytes):,} bytes in {read_dur:.2f}s")

    importer = TelegramImporter(db)
    t1 = time.time()
    import_res = importer.import_chat_json(raw_bytes)
    import_dur = time.time() - t1

    chat = import_res.chat
    report = import_res.report

    print(f"[{datetime.now(UTC).isoformat()}] Import completed in {import_dur:.2f}s")
    print(f"  Chat ID: {chat.id}")
    print(f"  Fingerprint: {chat.import_fingerprint}")
    print(f"  Total records: {report.total_records:,}")
    print(f"  Supported text messages: {report.supported_text_messages:,}")
    print(f"  Service records: {report.service_records:,}")
    print(f"  Unsupported media count: {report.unsupported_media_count:,}")
    print(f"  Earliest sent at: {report.earliest_sent_at}")
    print(f"  Latest sent at: {report.latest_sent_at}")

    # Validate exact counts
    assert report.total_records == EXPECTED_TOTAL_RECORDS, (
        f"Expected {EXPECTED_TOTAL_RECORDS} total records, got {report.total_records}"
    )
    assert report.supported_text_messages == EXPECTED_SUPPORTED_RECORDS, (
        f"Expected {EXPECTED_SUPPORTED_RECORDS} text, got {report.supported_text_messages}"
    )

    # Validate database state
    with db.connection() as conn:
        db_total = conn.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ?;", (chat.id,)
        ).fetchone()[0]
        db_supported = conn.execute(
            "SELECT count(*) FROM messages WHERE chat_id = ? AND is_supported_text = 1;",
            (chat.id,),
        ).fetchone()[0]
        assert db_total == report.total_records
        assert db_supported == report.supported_text_messages

    print(
        f"[{datetime.now(UTC).isoformat()}] Database verification passed: "
        f"{db_supported:,} supported messages indexed."
    )
    return chat.id


def estimate_and_display_plan(db, chat_id: str, chunk_size: int, overlap: int, max_calls: int):
    """Build plan, compute chunk counts, baseline merges, and projected token costs."""
    analysis = AnalysisService(db, chunk_size=chunk_size, overlap=overlap)
    estimate = analysis.estimate_chunks(chat_id, chunk_size=chunk_size, overlap=overlap)

    est_chunks = estimate.estimated_chunks
    est_merges = estimate.estimated_merge_calls
    est_total_calls = estimate.estimated_total_calls

    avg_chunk_input_tokens = 5000
    avg_chunk_output_tokens = 2000
    avg_merge_input_tokens = 7000
    avg_merge_output_tokens = 2500

    proj_input_tokens = (est_chunks * avg_chunk_input_tokens) + (
        est_merges * avg_merge_input_tokens
    )
    proj_output_tokens = (est_chunks * avg_chunk_output_tokens) + (
        est_merges * avg_merge_output_tokens
    )
    proj_total_tokens = proj_input_tokens + proj_output_tokens

    cost_input = (proj_input_tokens / 1_000_000) * INPUT_TOKEN_PRICE_PER_M
    cost_output = (proj_output_tokens / 1_000_000) * OUTPUT_TOKEN_PRICE_PER_M
    cost_total = cost_input + cost_output

    print("\n--- Plan & Cost Estimate ---")
    print(f"  Chunk size: {chunk_size} messages (overlap: {overlap})")
    print(f"  Total supported messages: {estimate.total_supported_messages:,}")
    print(f"  Estimated chunks: {est_chunks:,}")
    print(f"  Estimated merge calls: {est_merges:,}")
    print(f"  Estimated total calls: {est_total_calls:,}")
    print(f"  Configured call cap: {max_calls:,}")
    print("  Pricing assumptions (gemini-3.8-flash published rates through 2026):")
    print(
        f"    Input tokens: ${INPUT_TOKEN_PRICE_PER_M:.2f}/M, "
        f"Output tokens (incl thoughts): ${OUTPUT_TOKEN_PRICE_PER_M:.2f}/M"
    )
    print(f"    Projected input tokens: ~{proj_input_tokens:,} (~${cost_input:.2f})")
    print(f"    Projected output tokens: ~{proj_output_tokens:,} (~${cost_output:.2f})")
    print(f"    Projected total tokens: ~{proj_total_tokens:,}")
    print(f"    Projected total estimate: ~${cost_total:.2f}")
    print("----------------------------\n")

    return estimate


async def execute_analysis(
    db, chat_id: str, chunk_size: int, overlap: int, max_calls: int, concurrency: int = 8
):
    """Create or resume analysis job and execute under strict call cap."""
    analysis = AnalysisService(db, chunk_size=chunk_size, overlap=overlap)

    # Recover any stale in-flight claims before resuming
    analysis.recover_stale_jobs()

    # Check for active or resumable job
    existing = analysis.find_active_or_resumable_job(chat_id)
    if existing:
        jid = existing["id"]
        jstatus = existing["status"]
        jcomp = existing["completed_chunks"]
        jtot = existing["total_chunks"]
        jcalls = existing["provider_calls"]
        print(
            f"[{datetime.now(UTC).isoformat()}] Found existing job {jid} "
            f"(status: {jstatus}, completed: {jcomp}/{jtot}, calls: {jcalls})"
        )

    job = analysis.create_or_resume_job(
        chat_id=chat_id,
        max_calls=max_calls,
        chunk_size=chunk_size,
        overlap=overlap,
    )
    print(
        f"[{datetime.now(UTC).isoformat()}] Job {job.id} prepared "
        f"(status: {job.status}, cap: {job.max_calls}, chunk_size: {job.chunk_size}, "
        f"concurrency: {concurrency})"
    )

    provider = GeminiProvider(
        api_key=settings.get_gemini_api_key(),
        model_name=settings.gemini_model,
        timeout_seconds=max(180.0, settings.gemini_timeout_seconds),
        concurrency=concurrency,
    )

    stop_monitor = asyncio.Event()

    async def _progress_monitor():
        while not stop_monitor.is_set():
            try:
                await asyncio.wait_for(stop_monitor.wait(), timeout=15.0)
                break
            except TimeoutError:
                pass
            try:
                curr = analysis.get_job(chat_id, job.id)
                u = provider.cumulative_usage
                pct = (curr.completed_chunks / max(1, curr.total_chunks)) * 100.0
                print(
                    f"[{datetime.now(UTC).isoformat()}] Progress: "
                    f"chunks {curr.completed_chunks}/{curr.total_chunks} ({pct:.1f}%), "
                    f"calls {curr.provider_calls}/{curr.max_calls}, "
                    f"msgs {curr.processed_unique_messages:,}/{curr.total_supported_messages:,} | "
                    f"tokens total: {u['total_tokens']:,} (prompt: {u['prompt_tokens']:,}, "
                    f"output: {u['candidates_tokens']:,}, thoughts: {u['thoughts_tokens']:,})"
                )
            except Exception:
                pass

    monitor_task = asyncio.create_task(_progress_monitor())
    t0 = time.time()
    try:
        updated_job = await analysis.execute_job(
            chat_id=chat_id,
            job_id=job.id,
            provider=provider,
            concurrency=concurrency,
        )
        dur = time.time() - t0
        stop_monitor.set()
        await monitor_task

        u = provider.cumulative_usage
        c_input = (u["prompt_tokens"] / 1_000_000) * INPUT_TOKEN_PRICE_PER_M
        c_output = (
            (u["candidates_tokens"] + u["thoughts_tokens"]) / 1_000_000
        ) * OUTPUT_TOKEN_PRICE_PER_M
        c_total = c_input + c_output

        print(f"\n[{datetime.now(UTC).isoformat()}] Execution loop ended in {dur:.1f}s")
        print(f"  Job status: {updated_job.status}")
        print(f"  Chunks completed: {updated_job.completed_chunks}/{updated_job.total_chunks}")
        print(
            f"  Unique messages processed: "
            f"{updated_job.processed_unique_messages:,}/{updated_job.total_supported_messages:,}"
        )
        print(f"  Provider calls made: {updated_job.provider_calls}/{updated_job.max_calls}")
        if updated_job.last_error_code:
            print(f"  Last error code: {updated_job.last_error_code}")

        print("\n--- Actual Inference Token Accounting ---")
        print(f"  Prompt tokens: {u['prompt_tokens']:,} (${c_input:.4f})")
        print(f"  Candidates tokens: {u['candidates_tokens']:,}")
        print(f"  Thoughts tokens: {u['thoughts_tokens']:,}")
        print(f"  Total tokens: {u['total_tokens']:,}")
        print(f"  Total estimated cost: ${c_total:.4f}")
        print("----------------------------------------\n")

        # Validate profile if completed
        if updated_job.status == "completed":
            profile = analysis.get_latest_profile(chat_id)
            if profile:
                print("--- Profile Validation ---")
                print(f"  Profile version: {profile.version}")
                print(f"  Instructions length: {len(profile.instructions_md)} chars")
                print(f"  Memes count: {len(profile.memes)}")
                print(f"  Tone/register: {profile.structured.get('tone_and_register')}")
                print(f"  Languages: {profile.structured.get('languages')}")
                # Verify meme evidence
                with db.connection() as conn:
                    all_ev_ids = [eid for m in profile.memes for eid in m.evidence_message_ids]
                    if all_ev_ids:
                        placeholders = ",".join("?" for _ in all_ev_ids)
                        query = (
                            "SELECT count(*) FROM messages WHERE chat_id = ? "
                            f"AND source_message_id IN ({placeholders});"
                        )
                        found = conn.execute(query, (chat_id, *all_ev_ids)).fetchone()[0]
                        total_ev = len(all_ev_ids)
                        print(f"  Meme evidence attribution: {found}/{total_ev} valid IDs in DB")
                print("--------------------------\n")

        return updated_job

    finally:
        stop_monitor.set()
        await monitor_task
        await provider.close()


async def run_playground_replies(db, chat_id: str):
    """Execute at most 2 real generated playground replies using the learned profile."""
    analysis = AnalysisService(db)
    retriever = MemoryRetriever(db)
    reply_service = ReplyService(db, retriever, analysis)

    profile = analysis.get_latest_profile(chat_id)
    if not profile:
        print("No profile available for playground replies.")
        return

    print("\n--- Diagnostic Playground Replies (Capped at 2 calls) ---")
    provider = GeminiProvider(
        api_key=settings.get_gemini_api_key(),
        model_name=settings.gemini_model,
        timeout_seconds=30.0,
    )

    try:
        # Reply 1: Auto mode
        req1 = ReplyRequest(context="Привіт! Що нового в чаті?", mode="auto")
        rep1 = await reply_service.generate_reply(chat_id, req1, provider=provider)
        qid = rep1.quote.id if rep1.quote else None
        gen_len = len(rep1.text) if rep1.text else 0
        print("Reply 1 (Auto):")
        print(f"  Action: {rep1.action}")
        print(f"  Quote selected: {rep1.quote is not None} (ID: {qid})")
        print(f"  Generated text present: {bool(rep1.text)} (length: {gen_len})")
        print(f"  Evidence IDs count: {len(rep1.evidence_ids)}")

        # Reply 2: Generate mode
        req2 = ReplyRequest(context="Розкажи якийсь локальний мем нашої групи", mode="generate")
        rep2 = await reply_service.generate_reply(chat_id, req2, provider=provider)
        gen2_len = len(rep2.text) if rep2.text else 0
        print("Reply 2 (Generate):")
        print(f"  Action: {rep2.action}")
        print(f"  Quote selected: {rep2.quote is not None}")
        print(f"  Generated text present: {bool(rep2.text)} (length: {gen2_len})")
        print(f"  Evidence IDs count: {len(rep2.evidence_ids)}")

    finally:
        await provider.close()


async def check_telegram_bootstrap(db, chat_id: str):
    """Check Telegram setup and allowlist status."""
    print("\n--- Telegram Test Group Status ---")
    if not settings.is_telegram_configured:
        print("Status: TELEGRAM_BOT_TOKEN not configured.")
        return "unconfigured"

    from app.telegram import build_telegram_bot_service

    service = build_telegram_bot_service(db=db)
    try:
        me = await service.initialize()
        print(f"Bot user: @{me.get('username')} (ID: {me.get('id')})")
        allowed = list(service.allowed_chat_ids)
        print(f"Allowlisted chat IDs: {allowed if allowed else 'NONE (empty allowlist)'}")

        if not allowed:
            # Check if user posted marker command in updates
            updates = await service.transport.get_updates(offset=None, timeout=2)
            candidates = []
            for u in updates:
                msg = u.get("message", {})
                chat = msg.get("chat", {})
                text = (msg.get("text") or "").strip()
                if "/setup_test_group" in text or "/bind_vkyd" in text:
                    candidates.append((chat.get("id"), chat.get("type")))

            if len(candidates) == 1:
                cid, ctype = candidates[0]
                print(f"Detected unique setup marker from chat ID: {cid} (type: {ctype})")
                print("Ready to bind. Update TELEGRAM_ALLOWED_CHAT_IDS in .env.")
                return "marker_detected"
            elif len(candidates) > 1:
                print(f"Ambiguous setup markers found ({len(candidates)}). Wait for user clarity.")
                return "telegram_setup_required"
            else:
                print("No setup marker found in pending updates.")
                print("Status: telegram_setup_required")
                print("Instruction: add the bot to your test group and send /setup_test_group")
                return "telegram_setup_required"
        else:
            print(f"Active allowlist configured: {allowed}")
            return "ready"
    finally:
        await service.transport.close()


async def main():
    parser = argparse.ArgumentParser(description="Task 04 Archive Analysis and Telegram Runner")
    parser.add_argument("--import-only", action="store_true", help="Only run import and estimate")
    parser.add_argument("--estimate-only", action="store_true", help="Only compute plan estimate")
    parser.add_argument(
        "--run-analysis", action="store_true", help="Run the actual Gemini analysis"
    )
    parser.add_argument(
        "--telegram-check", action="store_true", help="Check Telegram test group readiness"
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="Concurrency for bounded worker pool (default: 8)",
    )
    args = parser.parse_args()

    db = get_db()
    db.run_migrations()

    chunk_size = settings.analysis_chunk_size  # 500
    overlap = settings.analysis_context_overlap  # 10
    max_calls = settings.gemini_max_calls_per_analysis  # 1000

    # 1. Import
    chat_id = run_import(db, ARCHIVE_PATH)

    # 2. Estimate
    estimate_and_display_plan(db, chat_id, chunk_size, overlap, max_calls)
    if args.import_only or args.estimate_only:
        return

    # 3. Analysis execution
    if args.run_analysis:
        job = await execute_analysis(
            db, chat_id, chunk_size, overlap, max_calls, concurrency=args.concurrency
        )
        if job.status == "completed":
            await run_playground_replies(db, chat_id)

    # 4. Telegram check
    if args.telegram_check:
        await check_telegram_bootstrap(db, chat_id)


if __name__ == "__main__":
    asyncio.run(main())
