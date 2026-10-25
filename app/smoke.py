"""Opt-in tiny synthetic live Gemini smoke command with structured output validation."""

import asyncio
import json
import sys
from typing import Any

from app.config import settings
from app.providers.base import (
    ChunkAnalysisResult,
    MergeProfileResult,
    ProviderReplyDecision,
)
from app.providers.gemini import GeminiProvider

SYNTHETIC_SMOKE_MESSAGES = [
    {
        "source_message_id": 1,
        "sender_name": "SyntheticAlice",
        "sent_at": "2026-10-25T00:00:00Z",
        "text": "Привіт усім! Хтось хоче подвійне еспресо?",
        "is_supported_text": 1,
    },
    {
        "source_message_id": 2,
        "sender_name": "SyntheticBob",
        "sent_at": "2026-10-25T00:01:00Z",
        "text": "Тільки якщо зі сметаною, як завжди!",
        "is_supported_text": 1,
    },
]


async def run_smoke_test() -> int:
    """
    Execute at most ONE actual inference HTTP request with existing credentials.
    Exercises the production structured-output path (analyze_chunk) on two synthetic messages.
    Disables automatic retries (max_retries=0) and bounds deadline (20s) and tokens (1024).
    Reports only sanitized model, schema validation, and error category without secrets.
    """
    if not settings.is_provider_configured:
        result = {
            "status": "unconfigured",
            "model": settings.gemini_model,
            "attempted": False,
            "schema_validation": "skipped",
            "message": "Gemini API key is not configured in environment.",
        }
        print(json.dumps(result, indent=2))
        return 0

    provider = GeminiProvider(
        api_key=settings.get_gemini_api_key(),
        model_name=settings.gemini_model,
        timeout_seconds=20.0,
        max_retries=0,  # Strict single attempt; no application or transport retries
    )

    try:
        # Single bounded structured output attempt
        chunk_res: ChunkAnalysisResult = await provider.analyze_chunk(
            messages=SYNTHETIC_SMOKE_MESSAGES,
            chat_name="Synthetic Smoke Chat",
            max_output_tokens=1024,
        )

        schema_valid = isinstance(chunk_res, ChunkAnalysisResult)
        output: dict[str, Any] = {
            "status": "ok",
            "model": settings.gemini_model,
            "attempted": True,
            "schema_validation": "passed" if schema_valid else "failed",
            "style_detected": bool(chunk_res.style.languages),
            "memes_count": len(chunk_res.memes),
        }
        print(json.dumps(output, indent=2))
        return 0

    except Exception as e:
        code = getattr(e, "code", "provider_error")
        status_code = getattr(e, "status_code", 502)
        message = getattr(e, "message", "Provider smoke call failed.")

        output = {
            "status": "error",
            "model": settings.gemini_model,
            "attempted": True,
            "schema_validation": "failed",
            "error_category": code,
            "status_code": status_code,
            "message": message,
        }
        print(json.dumps(output, indent=2))
        return 1

    finally:
        await provider.close()


async def run_synthetic_live_sequence(max_attempts: int = 6) -> dict[str, Any]:
    """Execute a tiny synthetic structured sequence: analyze -> merge -> reply.

    Uses the real configured model and server-side credentials with a strict cap of at most
    max_attempts total. Disables automatic retries, bounds output tokens, and safely inspects
    finish/usage metadata without printing raw private text or secrets.
    """
    if not settings.is_provider_configured:
        return {
            "status": "unconfigured",
            "model": settings.gemini_model,
            "max_allowed_attempts": max_attempts,
            "attempts_made": 0,
            "dispatched_calls": 0,
            "schema_valid_calls": 0,
            "message": "Gemini API key is not configured in environment.",
        }

    provider = GeminiProvider(
        api_key=settings.get_gemini_api_key(),
        model_name=settings.gemini_model,
        timeout_seconds=30.0,
        max_retries=0,
    )

    results: dict[str, Any] = {
        "status": "pending",
        "model": settings.gemini_model,
        "max_allowed_attempts": max_attempts,
        "attempts_made": 0,
        "dispatched_calls": 0,
        "schema_valid_calls": 0,
        "operations": {},
        "token_usage": {
            "prompt_tokens": 0,
            "candidates_tokens": 0,
            "thoughts_tokens": 0,
            "total_tokens": 0,
        },
        "errors": [],
    }

    def _accumulate_usage():
        usage = getattr(provider, "last_usage_metadata", {})
        for k in ["prompt_tokens", "candidates_tokens", "thoughts_tokens", "total_tokens"]:
            results["token_usage"][k] += usage.get(k, 0)

    try:
        # Step 1: analyze_chunk
        if results["attempts_made"] >= max_attempts:
            results["status"] = "budget_exhausted"
            return results

        results["attempts_made"] += 1
        results["dispatched_calls"] += 1
        chunk_res: ChunkAnalysisResult = await provider.analyze_chunk(
            messages=SYNTHETIC_SMOKE_MESSAGES,
            chat_name="Synthetic Sequence Chat",
            max_output_tokens=4096,
        )
        if isinstance(chunk_res, ChunkAnalysisResult):
            results["schema_valid_calls"] += 1
            _accumulate_usage()
            results["operations"]["analyze_chunk"] = {
                "status": "passed",
                "memes_count": len(chunk_res.memes),
                "style_detected": bool(chunk_res.style.languages),
            }

        # Step 2: merge_findings
        if results["attempts_made"] >= max_attempts:
            results["status"] = "budget_exhausted"
            return results

        results["attempts_made"] += 1
        results["dispatched_calls"] += 1
        merge_res: MergeProfileResult = await provider.merge_findings(
            chunk_findings=[chunk_res],
            current_profile=None,
            chat_name="Synthetic Sequence Chat",
            max_output_tokens=8192,
        )
        if isinstance(merge_res, MergeProfileResult):
            results["schema_valid_calls"] += 1
            _accumulate_usage()
            results["operations"]["merge_findings"] = {
                "status": "passed",
                "memes_count": len(merge_res.memes),
                "instructions_len": len(merge_res.instructions_md),
            }

        # Step 3: decide_reply
        if results["attempts_made"] >= max_attempts:
            results["status"] = "budget_exhausted"
            return results

        results["attempts_made"] += 1
        results["dispatched_calls"] += 1
        reply_res: ProviderReplyDecision = await provider.decide_reply(
            context="Хто хоче каву зі сметаною?",
            candidate_quotes=SYNTHETIC_SMOKE_MESSAGES,
            profile_instructions=merge_res.instructions_md,
            mode="auto",
            max_output_tokens=2048,
        )
        if isinstance(reply_res, ProviderReplyDecision):
            results["schema_valid_calls"] += 1
            _accumulate_usage()
            results["operations"]["decide_reply"] = {
                "status": "passed",
                "action": reply_res.action,
                "quote_selected": reply_res.quote_source_message_id is not None,
                "has_generated_text": bool(reply_res.text),
            }

        results["status"] = "ok"
        return results

    except Exception as e:
        code = getattr(e, "code", "provider_error")
        status_code = getattr(e, "status_code", 502)
        message = getattr(e, "message", str(e))
        results["status"] = "error"
        results["errors"].append(
            {
                "error_category": code,
                "status_code": status_code,
                "message": message,
            }
        )
        return results

    finally:
        await provider.close()


def main() -> None:
    code = asyncio.run(run_smoke_test())
    sys.exit(code)


if __name__ == "__main__":
    main()
