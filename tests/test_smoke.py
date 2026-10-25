"""Tests for the opt-in tiny synthetic live Gemini smoke command."""

import json
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr

from app.config import settings
from app.providers.base import (
    ChunkAnalysisResult,
    MemeCandidate,
    ProviderError,
    StyleAnalysis,
)
from app.smoke import run_smoke_test


@pytest.mark.anyio
async def test_smoke_command_unconfigured_provider(capsys):
    """Smoke test prints unconfigured status without calling network when key is absent."""
    with patch.object(settings, "gemini_api_key", None):
        code = await run_smoke_test()
        assert code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "unconfigured"
        assert data["attempted"] is False
        assert data["schema_validation"] == "skipped"
        assert "not configured" in data["message"]


@pytest.mark.anyio
async def test_smoke_command_success(capsys):
    """Smoke test exercises structured analyze_chunk and validates returned schema."""
    fake_chunk_res = ChunkAnalysisResult(
        style=StyleAnalysis(languages=["Ukrainian"]),
        memes=[
            MemeCandidate(
                label="Smoke Meme",
                description="Synthetic joke",
                usage_context="Testing",
                evidence_source_message_ids=[1],
                confidence=0.9,
            )
        ],
        instructions_summary="Witty bot",
    )
    with (
        patch.object(settings, "gemini_api_key", SecretStr("synthetic-key")),
        patch(
            "app.smoke.GeminiProvider.analyze_chunk",
            new_callable=AsyncMock,
            return_value=fake_chunk_res,
        ),
    ):
        code = await run_smoke_test()
        assert code == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "ok"
        assert data["attempted"] is True
        assert data["schema_validation"] == "passed"
        assert data["model"] == settings.gemini_model
        assert data["memes_count"] == 1
        # Ensure no credential or auth token is in output
        assert "synthetic-key" not in captured.out


@pytest.mark.anyio
async def test_smoke_command_sanitizes_errors(capsys):
    """Smoke test sanitizes provider exceptions and returns error exit code."""
    with (
        patch.object(settings, "gemini_api_key", SecretStr("synthetic-key")),
        patch(
            "app.smoke.GeminiProvider.analyze_chunk",
            new_callable=AsyncMock,
            side_effect=ProviderError("auth_error", "Authentication failed", status_code=401),
        ),
    ):
        code = await run_smoke_test()
        assert code == 1
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["status"] == "error"
        assert data["attempted"] is True
        assert data["schema_validation"] == "failed"
        assert data["error_category"] == "auth_error"
        assert data["status_code"] == 401
