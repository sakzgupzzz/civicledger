"""Unified ticker profile — one call merging every CivicLedger source.

Combines fundamentals, insider trades, recent filings (incl. 8-K material
events), congressional trades, and top-institution holders for a single
ticker. Sub-fetches run concurrently and are individually fault-tolerant: a
failure in one source returns empty for that section rather than failing the
whole profile.
"""

import asyncio
from datetime import date
from typing import Any, Dict, List

from loguru import logger

from civicledger.cache import cached


async def _safe(coro, default):
    try:
        return await coro
    except Exception as e:  # noqa: BLE001
        logger.debug(f"profile sub-fetch failed: {e}")
        return default


async def _institutional_holders(ticker: str) -> List[Dict[str, Any]]:
    """Which tracked top institutions hold this ticker (from cached summary)."""
    from civicledger.edgar.institutional import fetch_top_institutions_summary
    summary = await cached("institutions/top", 21600, fetch_top_institutions_summary)
    holders = []
    for fund in summary or []:
        for h in fund.get("top_holdings", []):
            if (h.get("ticker") or "").upper() == ticker:
                holders.append({
                    "manager_name": fund.get("manager_name"),
                    "manager_cik": fund.get("manager_cik"),
                    "shares": h.get("shares"),
                    "value_usd": h.get("value_usd"),
                    "period": fund.get("period"),
                })
    holders.sort(key=lambda x: x.get("value_usd") or 0, reverse=True)
    return holders


async def _congress_for_ticker(ticker: str) -> List[Dict[str, Any]]:
    from civicledger.congress.trades import fetch_house_trades_detailed
    yr = date.today().year
    trades = await cached(
        f"house/{yr}/detail/90", 3600,
        lambda: fetch_house_trades_detailed(year=yr, limit=400, max_pdf_parse=90),
    )
    return [t for t in (trades or []) if (t.get("ticker") or "").upper() == ticker][:25]


async def fetch_ticker_profile(ticker: str) -> Dict[str, Any]:
    """Build a unified profile for one ticker across all sources."""
    tk = ticker.upper()
    from civicledger.edgar.fundamentals import fetch_fundamentals_for_ticker
    from civicledger.edgar.filings import fetch_company_filings
    from civicledger.edgar.insider_trades import fetch_insider_trades_detailed

    fundamentals, insider, filings, congress, holders = await asyncio.gather(
        _safe(cached(f"fundamentals/{tk}", 21600, lambda: fetch_fundamentals_for_ticker(tk)), None),
        _safe(cached(f"insider-detailed/{tk}/12", 1800, lambda: fetch_insider_trades_detailed(tk, limit=12)), []),
        _safe(cached(f"filings/{tk}/all/15", 3600, lambda: fetch_company_filings(tk, limit=15)), []),
        _safe(_congress_for_ticker(tk), []),
        _safe(_institutional_holders(tk), []),
    )

    # Material events = recent 8-Ks (which carry item codes) from the filing feed.
    material = [f for f in (filings or []) if (f.get("form") or "").startswith("8-K") and f.get("items")][:10]

    company = (fundamentals or {}).get("company") if fundamentals else None
    if not company:
        for src in (insider or []):
            if src.get("company"):
                company = src.get("company")
                break

    return {
        "ticker": tk,
        "company": company,
        "fundamentals": fundamentals,
        "insider_trades": insider or [],
        "material_events": material,
        "recent_filings": filings or [],
        "congress_trades": congress or [],
        "institutional_holders": holders or [],
        "sources": {
            "fundamentals": bool(fundamentals),
            "insider_trades": len(insider or []),
            "recent_filings": len(filings or []),
            "congress_trades": len(congress or []),
            "institutional_holders": len(holders or []),
        },
    }
