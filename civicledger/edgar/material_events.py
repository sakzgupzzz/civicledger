"""SEC EDGAR Material Events — 8-K filing RSS feed.

8-K filings report material corporate events: earnings, mergers,
leadership changes, lawsuits, guidance updates, etc. The SEC
publishes an RSS feed updated every 10 minutes.

Item codes:
- 1.01: Entry into a Material Agreement
- 1.02: Termination of a Material Agreement
- 2.01: Completion of Acquisition/Disposition
- 2.02: Results of Operations (earnings)
- 2.05: Costs of Exit/Restructuring
- 2.06: Material Impairments
- 3.01: Delisting / Transfer
- 4.01: Changes in Accountant
- 5.02: Departure/Appointment of Officers
- 5.03: Amendments to Articles/Bylaws
- 7.01: Regulation FD Disclosure
- 8.01: Other Events
- 9.01: Financial Statements and Exhibits

Source: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=8-K
Public domain.
"""

import re
from typing import Any, Dict, List, Optional

from loguru import logger

from civicledger.edgar._client import efts_search

TICKER_RE = re.compile(r"\(([A-Z]{1,5})\)")

# Human-readable item descriptions
ITEM_LABELS = {
    "1.01": "Material Agreement",
    "1.02": "Agreement Terminated",
    "2.01": "Acquisition/Disposition",
    "2.02": "Earnings Announcement",
    "2.05": "Restructuring",
    "2.06": "Material Impairment",
    "3.01": "Delisting/Transfer",
    "4.01": "Accountant Change",
    "5.02": "Officer Change",
    "5.03": "Bylaws Amendment",
    "7.01": "Regulation FD",
    "8.01": "Other Events",
    "9.01": "Financial Exhibits",
}


async def fetch_material_events(
    from_date: str,
    to_date: str,
    item_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Fetch material corporate events from 8-K filings.

    Args:
        from_date: Start date (YYYY-MM-DD)
        to_date: End date (YYYY-MM-DD)
        item_filter: Optional item code to filter (e.g., "5.02" for officer changes)

    Returns list of {ticker, company, filing_date, items, item_labels, cik}.
    """
    query = f'"Item {item_filter}"' if item_filter else '"8-K"'

    all_events: List[Dict[str, Any]] = []
    page = 0

    while True:
        data = await efts_search(
            query=query,
            forms="8-K",
            start_date=from_date,
            end_date=to_date,
            page=page,
            size=200,
        )

        if not data:
            break

        hits = data.get("hits", {}).get("hits", [])
        total = data.get("hits", {}).get("total", {}).get("value", 0)

        for h in hits:
            s = h.get("_source", {})
            filing_date = s.get("file_date")
            display_names = s.get("display_names", [])
            ciks = s.get("ciks", [])
            items = s.get("items", [])

            if item_filter and item_filter not in items:
                continue

            for i, name in enumerate(display_names):
                ticker_match = TICKER_RE.search(name or "")
                ticker = ticker_match.group(1) if ticker_match else None
                company = (name or "").split("(")[0].strip()

                item_labels = [
                    ITEM_LABELS.get(item, item) for item in items
                    if item != "9.01"  # Exclude boring "Financial Exhibits" from labels
                ]

                all_events.append({
                    "ticker": ticker,
                    "company": company,
                    "filing_date": filing_date,
                    "items": items,
                    "item_labels": item_labels,
                    "cik": int(ciks[i]) if i < len(ciks) else None,
                })

        fetched = (page + 1) * 200
        if fetched >= total or not hits:
            break
        page += 1

    # Dedupe by (company, filing_date, items)
    seen: set = set()
    deduped: List[Dict[str, Any]] = []
    for e in all_events:
        key = (e.get("company"), e.get("filing_date"), ",".join(e.get("items", [])))
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    deduped.sort(key=lambda x: x.get("filing_date", ""), reverse=True)
    logger.info(f"EDGAR material events: {len(deduped)} for {from_date} to {to_date}")
    return deduped
