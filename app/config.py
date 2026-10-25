"""Application configuration and redacted secret management."""

from pathlib import Path
from typing import Any

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Provider configuration
    gemini_api_key: SecretStr | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-3.8-flash", alias="GEMINI_MODEL")
    gemini_analysis_concurrency: int = Field(default=16, alias="GEMINI_ANALYSIS_CONCURRENCY")
    gemini_max_calls_per_analysis: int = Field(default=50, alias="GEMINI_MAX_CALLS_PER_ANALYSIS")
    gemini_timeout_seconds: float = Field(default=30.0, alias="GEMINI_TIMEOUT_SECONDS")
    gemini_max_retries: int = Field(default=0, alias="GEMINI_MAX_RETRIES")

    # App server and storage
    app_host: str = Field(default="127.0.0.1", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    app_data_dir: str = Field(default="data", alias="APP_DATA_DIR")
    db_filename: str = Field(default="vkyd.db", alias="DB_FILENAME")
    max_import_size_bytes: int = Field(default=50 * 1024 * 1024, alias="MAX_IMPORT_SIZE_BYTES")
    analysis_chunk_size: int = Field(default=50, alias="ANALYSIS_CHUNK_SIZE")
    analysis_context_overlap: int = Field(default=10, alias="ANALYSIS_CONTEXT_OVERLAP")

    # Telegram bot configuration (optional for worker)
    telegram_bot_token: SecretStr | None = Field(default=None, alias="TELEGRAM_BOT_TOKEN")
    telegram_allowed_chat_ids: str = Field(default="", alias="TELEGRAM_ALLOWED_CHAT_IDS")

    def get_gemini_api_key(self) -> str | None:
        """Extract Gemini API key only at runtime integration boundaries."""
        return self.gemini_api_key.get_secret_value() if self.gemini_api_key is not None else None

    def get_telegram_bot_token(self) -> str | None:
        """Extract Telegram bot token only at runtime integration boundaries."""
        return (
            self.telegram_bot_token.get_secret_value()
            if self.telegram_bot_token is not None
            else None
        )

    @property
    def is_provider_configured(self) -> bool:
        if self.gemini_api_key is None:
            return False
        val = self.gemini_api_key.get_secret_value()
        return bool(val and val.strip())

    @property
    def is_telegram_configured(self) -> bool:
        if self.telegram_bot_token is None:
            return False
        val = self.telegram_bot_token.get_secret_value()
        return bool(val and val.strip())

    @property
    def parsed_telegram_allowed_chat_ids(self) -> set[str]:
        if not self.telegram_allowed_chat_ids.strip():
            return set()
        return {item.strip() for item in self.telegram_allowed_chat_ids.split(",") if item.strip()}

    def get_db_path(self) -> Path:
        data_path = Path(self.app_data_dir)
        data_path.mkdir(parents=True, exist_ok=True)
        return data_path / self.db_filename

    def safe_dict(self) -> dict[str, Any]:
        """Return non-sensitive configuration parameters safely."""
        return {
            "gemini_model": self.gemini_model,
            "gemini_analysis_concurrency": self.gemini_analysis_concurrency,
            "gemini_max_calls_per_analysis": self.gemini_max_calls_per_analysis,
            "gemini_timeout_seconds": self.gemini_timeout_seconds,
            "gemini_max_retries": self.gemini_max_retries,
            "app_host": self.app_host,
            "app_port": self.app_port,
            "app_data_dir": self.app_data_dir,
            "max_import_size_bytes": self.max_import_size_bytes,
            "analysis_chunk_size": self.analysis_chunk_size,
            "analysis_context_overlap": self.analysis_context_overlap,
            "provider_configured": self.is_provider_configured,
            "telegram_configured": self.is_telegram_configured,
            "allowed_chat_ids_count": len(self.parsed_telegram_allowed_chat_ids),
        }

    def __repr__(self) -> str:
        return (
            f"<Settings provider_configured={self.is_provider_configured} "
            f"telegram_configured={self.is_telegram_configured}>"
        )

    def __str__(self) -> str:
        return self.__repr__()


# Global cached settings instance
settings = Settings()
