"""Tests for environment settings and secret redaction conforming to R2."""

from app.config import Settings


def test_settings_defaults():
    s = Settings(
        _env_file=None,
        GEMINI_API_KEY=None,
        TELEGRAM_BOT_TOKEN=None,
    )
    assert s.app_host == "127.0.0.1"
    assert s.app_port == 8000
    assert not s.is_provider_configured
    assert not s.is_telegram_configured
    assert s.get_gemini_api_key() is None
    assert s.get_telegram_bot_token() is None
    assert len(s.parsed_telegram_allowed_chat_ids) == 0


def test_settings_secret_redaction():
    secret_key = "AIzaSySyntheticTestKey9876543210"
    token = "987654:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"
    s = Settings(
        _env_file=None,
        GEMINI_API_KEY=secret_key,
        TELEGRAM_BOT_TOKEN=token,
        TELEGRAM_ALLOWED_CHAT_IDS="-100123,-100456",
    )
    assert s.is_provider_configured
    assert s.is_telegram_configured
    assert "-100123" in s.parsed_telegram_allowed_chat_ids

    # 1. str() and repr() must NEVER contain raw secrets
    str_repr = str(s)
    repr_str = repr(s)
    assert secret_key not in str_repr
    assert token not in str_repr
    assert secret_key not in repr_str
    assert token not in repr_str

    # 2. JSON serialization (model_dump_json) must mask secrets
    json_dump = s.model_dump_json()
    assert secret_key not in json_dump
    assert token not in json_dump
    assert '"gemini_api_key":"**********"' in json_dump
    assert '"telegram_bot_token":"**********"' in json_dump

    # 3. Public safe view (safe_dict) must contain boolean flags only
    safe_info = s.safe_dict()
    assert secret_key not in str(safe_info)
    assert token not in str(safe_info)
    assert safe_info["provider_configured"] is True
    assert safe_info["telegram_configured"] is True

    # 4. Explicit credential extraction only at runtime integration boundaries
    assert s.get_gemini_api_key() == secret_key
    assert s.get_telegram_bot_token() == token


def test_settings_empty_or_whitespace_secrets():
    s = Settings(
        _env_file=None,
        GEMINI_API_KEY="   ",
        TELEGRAM_BOT_TOKEN="   ",
    )
    assert not s.is_provider_configured
    assert not s.is_telegram_configured
    assert s.get_gemini_api_key() == "   "
    assert s.get_telegram_bot_token() == "   "
