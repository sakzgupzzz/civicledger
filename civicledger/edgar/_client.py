"""Shared async HTTP client for SEC EDGAR APIs.

Rate-limited to 10 req/sec as required by SEC. User-Agent header
is mandatory and must include a contact email.
"""

import asyncio
from typing import Any, Dict, Optional

import httpx
from loguru import logger

from civicledger.config import get_settings

_EDGAR_BASE = "https://data.sec.gov"
_EFTS_BASE = "https://efts.sec.gov/LATEST"


async def edgar_get(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    base: str = _EDGAR_BASE,
    timeout: float = 30,
) -> Optional[dict]:
    """Make a rate-limited GET request to SEC EDGAR.

    Returns parsed JSON or None on error.
    """
    settings = get_settings()
    url = f"{base}{path}"
    headers = {"User-Agent": settings.edgar_identity}

    await asyncio.sleep(settings.edgar_rate_limit)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        logger.debug(f"EDGAR request failed: {url} — {e}")
        return None


async def efts_search(
    query: str,
    forms: str = "8-K",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 0,
    size: int = 200,
) -> Optional[dict]:
    """Search EDGAR Full-Text Search (EFTS) API."""
    params: Dict[str, Any] = {
        "q": query,
        "forms": forms,
        "from": page * size,
        "size": size,
    }
    if start_date and end_date:
        params["dateRange"] = "custom"
        params["startdt"] = start_date
        params["enddt"] = end_date

    return await edgar_get(
        "/search-index",
        params=params,
        base=_EFTS_BASE,
        timeout=15,
    )


async def get_ticker_cik_map() -> Dict[str, int]:
    """Fetch ticker → CIK mapping from SEC company_tickers.json.

    Returns dict mapping uppercase ticker to CIK integer.
    """
    data = await edgar_get("/files/company_tickers.json")
    if not data:
        return {}

    result: Dict[str, int] = {}
    for entry in data.values():
        ticker = entry.get("ticker", "").upper()
        cik = entry.get("cik_str")
        if ticker and cik:
            result[ticker] = int(cik)

    logger.info(f"EDGAR ticker-CIK map: {len(result)} tickers")
    return result
