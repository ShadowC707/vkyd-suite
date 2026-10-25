"""Tests for AI providers (Gemini adapter error sanitization, retries, and FakeAIProvider)."""

from unittest.mock import AsyncMock, patch

import pytest

from app.providers.base import (
    BaseAIProvider,
    ChunkAnalysisResult,
    MergeProfileResult,
    ProviderError,
    ProviderReplyDecision,
)
from app.providers.fake import FakeAIProvider
from app.providers.gemini import GeminiProvider


@pytest.mark.anyio
async def test_fake_provider_deterministic_analysis_and_reply():
    """FakeAIProvider deterministically analyzes chunks and selects quotes."""
    provider = FakeAIProvider()
    assert isinstance(provider, BaseAIProvider)
    assert provider.provider_kind == "fake"

    messages = [
        {"source_message_id": 101, "sender_name": "Alice", "text": "Вареники готові!"},
        {"source_message_id": 102, "sender_name": "Bob", "text": "Вже біжу з сметаною."},
    ]
    chunk_res = await provider.analyze_chunk(messages, chat_name="Test Chat")
    assert isinstance(chunk_res, ChunkAnalysisResult)
    assert len(chunk_res.memes) == 1
    assert chunk_res.memes[0].evidence_source_message_ids == [101, 102]

    # Decide quote reply
    candidate_quotes = [
        {"source_message_id": 101, "sender_name": "Alice", "text": "Вареники готові!"}
    ]
    decision = await provider.decide_reply(
        context="Хто голодний?",
        candidate_quotes=candidate_quotes,
        profile_instructions="Speak Ukrainian",
        mode="quote",
    )
    assert isinstance(decision, ProviderReplyDecision)
    assert decision.action == "quote"
    assert decision.quote_source_message_id == 101


@pytest.mark.anyio
async def test_gemini_missing_credential_raises_clear_error():
    """Unconfigured Gemini provider raises credential_missing without calling API."""
    provider = GeminiProvider(api_key=None)
    with pytest.raises(ProviderError) as exc_info:
        await provider.analyze_chunk([], "Test Chat")
    assert exc_info.value.code == "credential_missing"
    assert "Gemini API key is not configured" in exc_info.value.message


def test_gemini_sanitizes_errors_never_leaking_secrets():
    """Provider error sanitization conceals raw URLs, request headers, and API keys."""
    provider = GeminiProvider(api_key="secret-key-12345", model_name="test-model")

    # Sensitive exception message containing key and authorization
    sensitive_exc = Exception(
        "Request to https://generativelanguage.googleapis.com/v1beta/models/"
        "test?key=secret-key-12345 "
        "failed with Authorization: Bearer secret-key-12345: 404 Model not found"
    )
    sanitized = provider._sanitize_error(sensitive_exc)
    assert isinstance(sanitized, ProviderError)
    assert sanitized.code == "model_unavailable"
    assert "secret-key-12345" not in sanitized.message
    assert "Authorization" not in sanitized.message
    assert "test-model" in sanitized.message

    # Quota exceeded sanitization
    quota_exc = Exception("ResourceExhausted: 429 Quota exceeded for quota metric")
    sanitized_quota = provider._sanitize_error(quota_exc)
    assert sanitized_quota.code == "quota_exceeded"
    assert sanitized_quota.retryable is True

    # Auth error sanitization
    auth_exc = Exception("API_KEY_INVALID: 401 PermissionDenied")
    sanitized_auth = provider._sanitize_error(auth_exc)
    assert sanitized_auth.code == "auth_error"
    assert sanitized_auth.retryable is False


@pytest.mark.anyio
async def test_gemini_disables_untracked_adapter_retries_to_protect_ledger():
    """Gemini adapter disables untracked retries so every dispatch is accounted by the ledger."""
    provider = GeminiProvider(
        api_key="synthetic-test-key",
        model_name="test-model",
        timeout_seconds=5.0,
        max_retries=2,  # Non-default setting must NOT bypass the ledger
        concurrency=1,
    )

    mock_client = AsyncMock()
    mock_client.aio.models.generate_content.side_effect = Exception("429 ResourceExhausted")

    with patch.object(provider, "_get_client", return_value=mock_client):
        with pytest.raises(ProviderError) as exc_info:
            await provider.analyze_chunk([], "Test Chat")

        assert exc_info.value.code == "quota_exceeded"
        # Strictly 1 invocation; never 3 untracked retries!
        assert mock_client.aio.models.generate_content.call_count == 1


@pytest.mark.anyio
async def test_gemini_rejects_empty_or_invalid_structured_output():
    """Empty or non-conforming responses raise invalid_response error."""
    provider = GeminiProvider(
        api_key="synthetic-test-key",
        model_name="test-model",
        max_retries=0,
    )

    mock_response = AsyncMock()
    mock_response.text = "   "  # Whitespace-only

    mock_client = AsyncMock()
    mock_client.aio.models.generate_content.return_value = mock_response

    with patch.object(provider, "_get_client", return_value=mock_client):
        with pytest.raises(ProviderError) as exc_info:
            await provider.analyze_chunk([], "Test Chat")

        assert exc_info.value.code == "invalid_response"


@pytest.mark.anyio
async def test_gemini_schema_validation_failure_does_not_leak_raw_output():
    """Invalid structured responses must not leak raw model output into public error message."""
    provider = GeminiProvider(
        api_key="synthetic-test-key",
        model_name="test-model",
        max_retries=0,
    )

    sensitive_token = "LEAKED_SECRET_INTERNAL_PAYLOAD_9999"
    # Return JSON that violates schema (e.g. style is a string instead of object)
    mock_response = AsyncMock()
    mock_response.text = f'{{"style": "{sensitive_token}", "memes": []}}'

    mock_client = AsyncMock()
    mock_client.aio.models.generate_content.return_value = mock_response

    with patch.object(provider, "_get_client", return_value=mock_client):
        with pytest.raises(ProviderError) as exc_info:
            await provider.analyze_chunk([], "Test Chat")

        assert exc_info.value.code == "invalid_response"
        # Public message must NEVER leak the raw model output string
        assert sensitive_token not in exc_info.value.message
        expected_msg = "Provider returned structured data that failed schema validation."
        assert exc_info.value.message == expected_msg


@pytest.mark.anyio
async def test_gemini_candidate_finish_reason_max_tokens_and_safety():
    """Verify finish_reason MAX_TOKENS and SAFETY raise typed errors."""
    provider = GeminiProvider(
        api_key="synthetic-test-key",
        model_name="test-model",
        max_retries=0,
    )

    mock_client = AsyncMock()

    # Case 1: MAX_TOKENS finish_reason
    cand_tokens = AsyncMock()
    cand_tokens.finish_reason = "MAX_TOKENS"
    mock_resp_tokens = AsyncMock()
    mock_resp_tokens.candidates = [cand_tokens]

    mock_client.aio.models.generate_content.return_value = mock_resp_tokens
    with patch.object(provider, "_get_client", return_value=mock_client):
        with pytest.raises(ProviderError) as exc_info:
            await provider.analyze_chunk([], "Test Chat")
        assert exc_info.value.code == "token_exhaustion"
        assert exc_info.value.retryable is True

    # Case 2: SAFETY finish_reason
    cand_safety = AsyncMock()
    cand_safety.finish_reason = "SAFETY"
    mock_resp_safety = AsyncMock()
    mock_resp_safety.candidates = [cand_safety]

    mock_client.aio.models.generate_content.return_value = mock_resp_safety
    with patch.object(provider, "_get_client", return_value=mock_client):
        with pytest.raises(ProviderError) as exc_info:
            await provider.analyze_chunk([], "Test Chat")
        assert exc_info.value.code == "safety_blocked"
        assert exc_info.value.retryable is False


@pytest.mark.anyio
async def test_gemini_400_sanitization_extracts_detail_without_secrets():
    """Verify HTTP 400 sanitization extracts diagnostic message while redacting secrets."""
    provider = GeminiProvider(api_key="synthetic-test-key", model_name="gemini-3.8-flash")

    # Simulate Google 400 ClientError with sensitive URL in message
    err_400 = Exception(
        "INVALID_ARGUMENT: Schema error at Field 'foo': unknown field. "
        "URL: https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-3.8-flash:generateContent?key=MY_SECRET_KEY_999"
    )
    err_400.code = 400
    err_400.status = "INVALID_ARGUMENT"

    sanitized = provider._sanitize_error(err_400)
    assert sanitized.code == "invalid_request"
    assert sanitized.status_code == 400
    assert "MY_SECRET_KEY_999" not in sanitized.message
    assert "key=[REDACTED]" in sanitized.message
    assert "Schema error at Field 'foo'" in sanitized.message


@pytest.mark.anyio
async def test_sdk_schema_conversion_developer_api_mode():
    """Verify Google SDK schema transformer succeeds in Developer API mode."""
    from google import genai
    from google.genai import _transformers

    from app.providers.base import (
        ChunkAnalysisResult,
        MergeProfileResult,
        ProviderReplyDecision,
    )

    client = genai.Client(api_key="synthetic-key")
    s_chunk = _transformers.t_schema(client._api_client, ChunkAnalysisResult)
    assert s_chunk is not None
    assert s_chunk.properties is not None

    s_merge = _transformers.t_schema(client._api_client, MergeProfileResult)
    assert s_merge is not None
    assert s_merge.properties is not None
    # structured must be typed StyleAnalysis, not rejected additionalProperties
    assert "structured" in s_merge.properties

    s_reply = _transformers.t_schema(client._api_client, ProviderReplyDecision)
    assert s_reply is not None
    assert s_reply.properties is not None


@pytest.mark.anyio
async def test_actual_sdk_serialization_through_mock_transport_all_three_operations():
    """Verify real Google SDK request construction, JSON serialization via MockTransport."""
    import json
    from typing import Any

    import httpx
    from google import genai
    from google.genai import types

    from app.providers.base import (
        ChunkAnalysisResult,
        MergeProfileResult,
        ProviderReplyDecision,
    )

    intercepted_requests: list[dict[str, Any]] = []

    def transport_handler(request: httpx.Request) -> httpx.Response:
        req_json = json.loads(request.content)
        intercepted_requests.append(
            {
                "url": str(request.url),
                "method": request.method,
                "json": req_json,
            }
        )

        # Distinguish operation from URL or prompt
        content_text = json.dumps(req_json)

        if "MergeProfileResult" in content_text or "Synthesize" in content_text:
            resp_body = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "instructions_md": (
                                                "# Synthesized Instructions\nSpeak with wit."
                                            ),
                                            "structured": {
                                                "languages": ["Ukrainian"],
                                                "reply_length": "short",
                                                "tone_and_register": "ironic",
                                                "humor_techniques": ["puns"],
                                                "recurring_themes": ["tech"],
                                            },
                                            "memes": [
                                                {
                                                    "label": "Merged Joke",
                                                    "description": "Synthesized meme",
                                                    "usage_context": "Banter",
                                                    "evidence_source_message_ids": [10, 20],
                                                    "confidence": 0.95,
                                                }
                                            ],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            }
        elif "Candidate Quote ID" in content_text or "ProviderReplyDecision" in content_text:
            resp_body = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "action": "quote",
                                            "quote_source_message_id": 42,
                                            "text": None,
                                            "evidence_source_message_ids": [42],
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            }
        else:
            # Chunk analysis
            resp_body = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json.dumps(
                                        {
                                            "style": {
                                                "languages": ["Ukrainian"],
                                                "reply_length": "short and punchy",
                                                "tone_and_register": "friendly",
                                                "humor_techniques": ["irony"],
                                                "recurring_themes": ["coffee"],
                                            },
                                            "memes": [
                                                {
                                                    "label": "Coffee Meme",
                                                    "description": "Morning coffee recurring joke",
                                                    "usage_context": "Mornings",
                                                    "evidence_source_message_ids": [1],
                                                    "confidence": 0.88,
                                                }
                                            ],
                                            "instructions_summary": "Morning banter rules.",
                                        }
                                    )
                                }
                            ]
                        }
                    }
                ]
            }
        return httpx.Response(200, json=resp_body)

    mock_transport = httpx.MockTransport(transport_handler)
    mock_async_client = httpx.AsyncClient(transport=mock_transport)

    http_opts = types.HttpOptions(
        httpx_async_client=mock_async_client,
        retry_options=types.HttpRetryOptions(attempts=1),
    )
    genai_client = genai.Client(api_key="synthetic-key", http_options=http_opts)

    provider = GeminiProvider(
        api_key="synthetic-key",
        client=genai_client,
    )

    try:
        # Operation 1: analyze_chunk
        chunk_res = await provider.analyze_chunk(
            messages=[{"source_message_id": 1, "sender_name": "Alice", "text": "Кава готова!"}],
            chat_name="Real SDK Test",
        )
        assert isinstance(chunk_res, ChunkAnalysisResult)
        assert len(chunk_res.memes) == 1
        assert chunk_res.memes[0].label == "Coffee Meme"

        # Operation 2: merge_findings
        merge_res = await provider.merge_findings(
            chunk_findings=[chunk_res],
            current_profile=None,
            chat_name="Real SDK Test",
        )
        assert isinstance(merge_res, MergeProfileResult)
        assert len(merge_res.memes) == 1
        assert merge_res.memes[0].label == "Merged Joke"
        assert merge_res.structured.tone_and_register == "ironic"

        # Operation 3: decide_reply
        reply_res = await provider.decide_reply(
            context="Хто п'є каву?",
            candidate_quotes=[{"source_message_id": 42, "sender_name": "Bob", "text": "Я!"}],
            profile_instructions="Be witty",
            mode="auto",
        )
        assert isinstance(reply_res, ProviderReplyDecision)
        assert reply_res.action == "quote"
        assert reply_res.quote_source_message_id == 42

        # Verify all 3 HTTP requests were serialized by the SDK with valid generationConfig schemas
        assert len(intercepted_requests) == 3
        for req in intercepted_requests:
            assert "contents" in req["json"]
            gen_cfg = req["json"].get("generationConfig", {})
            assert gen_cfg.get("responseMimeType") == "application/json"
            assert "responseSchema" in gen_cfg

    finally:
        await mock_async_client.aclose()


@pytest.mark.anyio
async def test_to_strict_gemini_schema_strips_disallowed_keys():
    """Verify to_strict_gemini_schema strips default, title, max_length."""
    from google.genai import types

    from app.providers.gemini import to_strict_gemini_schema

    for cls in [ChunkAnalysisResult, MergeProfileResult, ProviderReplyDecision]:
        schema = to_strict_gemini_schema(cls)
        assert isinstance(schema, types.Schema)
        d = schema.model_dump(exclude_none=True)

        def assert_no_disallowed(node):
            if isinstance(node, dict):
                assert "title" not in node
                assert "default" not in node
                assert "max_length" not in node
                assert "maxLength" not in node
                for v in node.values():
                    assert_no_disallowed(v)
            elif isinstance(node, list):
                for item in node:
                    assert_no_disallowed(item)

        assert_no_disallowed(d)


@pytest.mark.anyio
async def test_gemini_extract_safe_detail_with_field_violations():
    """Verify _extract_safe_detail extracts fieldViolations from Google ClientError details."""
    provider = GeminiProvider(api_key="synthetic-test-key")
    err = Exception("Request contains an invalid argument.")
    err.code = 400
    err.details = {
        "error": {
            "code": 400,
            "message": "Request contains an invalid argument.",
            "status": "INVALID_ARGUMENT",
            "details": [
                {
                    "@type": "type.googleapis.com/google.rpc.BadRequest",
                    "fieldViolations": [
                        {
                            "field": "generation_config.thinking_config.thinking_level",
                            "description": "Thinking level LOW is not supported",
                        }
                    ],
                }
            ],
        }
    }
    detail = provider._extract_safe_detail(err)
    assert "fieldViolations:" in detail
    assert "thinking_level" in detail
    assert "LOW is not supported" in detail
