"""Configuration for CivicLedger."""

from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    """CivicLedger settings. All optional — works with zero config."""

    # SEC EDGAR (no API key needed, just identity for User-Agent)
    edgar_identity: str = "CivicLedger admin@civicledger.dev"

    # FRED (free API key from https://fred.stlouisfed.org/docs/api/api_key.html)
    fred_api_key: str = ""

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/civicledger.db"

    # Redis (optional — works without it, just no caching)
    redis_url: Optional[str] = None

    # Rate limiting
    edgar_rate_limit: float = 0.12  # seconds between requests (≈8/sec, under 10/sec limit)

    # Logging
    log_level: str = "INFO"

    class Config:
        env_prefix = "CIVICLEDGER_"
        env_file = ".env"
        extra = "ignore"


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
