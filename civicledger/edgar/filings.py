"""SEC EDGAR filing search and per-company filing feeds.

Two capabilities:
  - search_filings(): full-text search across all EDGAR filings (EFTS).
  - fetch_company_filings(): recent filings for one company, any form type.

Source: SEC EDGAR EFTS + submissions APIs. Public domain. No API key required.
"""

import asyncio
import os
import re
from typing import Any, Dict, List, Optional

from loguru import logger

from civicledger.config import get_settings
from civicledger.edgar._client import efts_search

_TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")


def _filing_index_url(accession: str, cik: Optional[int]) -> Optional[str]:
    """Build the EDGAR filing-index URL from an accession number."""
    if not accession or cik is None:
        return None
    acc_nodash = accession.replace("-", "")
    return (
        f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
        f"&CIK={cik}&type=&dateb=&owner=include&count=40"
        if not acc_nodash
        else f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{accession}-index.htm"
    )


async def search_filings(
    query: str,
    forms: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Full-text search across EDGAR filings.

    Args:
        query: Search phrase (e.g. "artificial intelligence", "going concern").
        forms: Optional comma-separated form types (e.g. "8-K,10-K"). Default all.
        from_date / to_date: Optional YYYY-MM-DD bounds.
        limit: Max results.

    Returns list of {company, ticker, form, filing_date, cik, accession, url}.
    """
    results: List[Dict[str, Any]] = []
    page = 0
    while len(results) < limit:
        data = await efts_search(
            query=query, forms=forms or "", start_date=from_date,
            end_date=to_date, page=page, size=min(100, limit),
        )
        if not data:
            break
        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {}).get("value", 0)
        for h in hits:
            s = h.get("_source", {})
            names = s.get("display_names", [])
            ciks = s.get("ciks", [])
            name0 = names[0] if names else ""
            m = _TICKER_RE.search(name0)
            accession = (h.get("_id", "").split(":")[0]) or s.get("adsh")
            cik = int(ciks[0]) if ciks else None
            results.append({
                "company": name0.split("(")[0].strip() or None,
                "ticker": m.group(1) if m else None,
                "form": ((s.get("root_forms") or [None])[0] or s.get("file_type")),
                "filing_date": s.get("file_date"),
                "cik": cik,
                "accession": accession,
                "url": _filing_index_url(accession, cik),
            })
            if len(results) >= limit:
                break
        if (page + 1) * min(100, limit) >= total or not hits:
            break
        page += 1
        await asyncio.sleep(0.12)

    logger.info(f"EDGAR filing search '{query}': {len(results)} hits")
    return results[:limit]


async def fetch_company_filings(
    ticker: str,
    form: Optional[str] = None,
    limit: int = 40,
) -> List[Dict[str, Any]]:
    """Fetch a company's recent filings (any form type) via edgartools.

    Args:
        ticker: Stock ticker (e.g. "AAPL").
        form: Optional form filter (e.g. "8-K", "10-K", "4").
        limit: Max filings.

    Returns list of {form, filing_date, accession, items, description, url}.
    """
    def _work() -> List[Dict[str, Any]]:
        from edgar import Company, set_identity

        os.environ.setdefault("EDGAR_LOCAL_CACHE", "/tmp/edgar_cache")
        set_identity(get_settings().edgar_identity)
        company = Company(ticker)
        filings = company.get_filings(form=form) if form else company.get_filings()
        if not filings:
            return []
        out: List[Dict[str, Any]] = []
        for f in list(filings)[:limit]:
            raw_items = getattr(f, "items", None)
            if isinstance(raw_items, str):
                items = re.findall(r"\d+\.\d+", raw_items) or None
            elif raw_items:
                items = [str(i) for i in raw_items]
            else:
                items = None
            out.append({
                "form": getattr(f, "form", None),
                "filing_date": str(getattr(f, "filing_date", "")),
                "accession": getattr(f, "accession_no", None),
                "items": items,
                "description": getattr(f, "primary_doc_description", None)
                or getattr(f, "primaryDocDescription", None),
                "url": getattr(f, "filing_url", None),
            })
        return out

    try:
        rows = await asyncio.to_thread(_work)
        logger.info(f"EDGAR filings for {ticker}: {len(rows)} ({form or 'all forms'})")
        return rows
    except ImportError:
        logger.warning("edgartools not installed — company filings unavailable")
        return []
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Company filings failed for {ticker}: {e}")
        return []
