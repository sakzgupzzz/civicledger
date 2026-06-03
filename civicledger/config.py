"""Configuration for CivicLedger."""

from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """CivicLedger settings. All optional — works with zero config."""

    model_config = SettingsConfigDict(
        env_prefix="CIVICLEDGER_", env_file=".env", extra="ignore"
    )

    # SEC EDGAR (no API key needed, just identity for User-Agent)
    edgar_identity: str = "CivicLedger admin@civicledger.dev"

    # FRED (free API key from https://fred.stlouisfed.org/docs/api/api_key.html)
    fred_api_key: str = ""

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/civicledger.db"

    # DynamoDB
    dynamodb_table: str = "civicledger"
    dynamodb_region: str = "us-east-1"

    # Rate limiting
    edgar_rate_limit: float = 0.12  # seconds between requests (≈8/sec, under 10/sec limit)

    # Local disk cache (so repeated dashboard/API loads are instant)
    cache_dir: str = "~/.cache/civicledger"
    cache_enabled: bool = True

    # Logging
    log_level: str = "INFO"


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
