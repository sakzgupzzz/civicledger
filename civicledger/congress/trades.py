"""Congressional stock trades from House clerk bulk XML downloads.

House: https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip
Senate: https://efdsearch.senate.gov/ (blocks automated access — not used here)

The House clerk provides annual ZIP files containing XML with all financial
disclosures including Periodic Transaction Reports (PTRs). This is public
data under the STOCK Act (2012).

Note: Senate data requires either manual access or a different data source.
The Senate eFD site actively blocks Lambda/server IPs.
"""

import asyncio
import io
import re
import zipfile
from datetime import date
from typing import Any, Dict, List, Optional
from xml.etree import ElementTree

import httpx
from loguru import logger

HOUSE_ZIP_URL = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"


async def fetch_house_trades(
    year: Optional[int] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch House member financial disclosures from the bulk XML download.

    Downloads the annual ZIP, parses XML for Periodic Transaction Reports,
    and extracts trade details.

    Args:
        year: Year to fetch. Defaults to current year.
        limit: Max results to return.

    Returns list of {politician, chamber, state, disclosure_date,
    doc_id, filing_type, source_url}.
    """
    if year is None:
        year = date.today().year

    url = HOUSE_ZIP_URL.format(year=year)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CivicLedger/0.1; +https://github.com/sakzgupzzz/civicledger)",
    }

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning(f"House ZIP download failed: {resp.status_code} for {url}")
                return []

            # Parse ZIP in memory
            zip_data = io.BytesIO(resp.content)
            trades: List[Dict[str, Any]] = []

            with zipfile.ZipFile(zip_data) as zf:
                for name in zf.namelist():
                    if not name.endswith(".xml"):
                        continue

                    try:
                        xml_content = zf.read(name)
                        root = ElementTree.fromstring(xml_content)

                        # Parse each Member element
                        for member in root.iter("Member"):
                            prefix = member.findtext("Prefix", "").strip()
                            last = member.findtext("Last", "").strip()
                            first = member.findtext("First", "").strip()
                            suffix = member.findtext("Suffix", "").strip()
                            filing_type = member.findtext("FilingType", "").strip()
                            state_dst = member.findtext("StateDst", "").strip()
                            filing_date = member.findtext("FilingDate", "").strip()
                            doc_id = member.findtext("DocID", "").strip()

                            # Only include PTRs (Periodic Transaction Reports)
                            if filing_type.upper() not in ("P", "PTR"):
                                continue

                            name_parts = [p for p in [prefix, first, last, suffix] if p]
                            full_name = " ".join(name_parts)

                            # Extract state from StateDst (e.g., "CA05" → "CA")
                            state_match = re.match(r"([A-Z]{2})", state_dst)
                            state = state_match.group(1) if state_match else None

                            pdf_url = (
                                f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"
                                if doc_id else None
                            )

                            trades.append({
                                "politician": full_name,
                                "chamber": "house",
                                "party": None,  # Not in XML
                                "state": state,
                                "disclosure_date": filing_date,
                                "transaction_date": None,  # Would need to parse the PDF
                                "asset_description": None,
                                "ticker": None,  # Would need to parse the PDF
                                "transaction_type": None,
                                "amount_range": None,
                                "doc_id": doc_id,
                                "source_url": pdf_url,
                            })

                            if len(trades) >= limit:
                                break
                    except ElementTree.ParseError as e:
                        logger.debug(f"XML parse error in {name}: {e}")
                        continue

            # Sort by disclosure date descending
            trades.sort(key=lambda t: t.get("disclosure_date", ""), reverse=True)
            logger.info(f"House trades: {len(trades)} PTRs for {year}")
            return trades[:limit]

    except Exception as e:
        logger.warning(f"House trades fetch failed: {e}")
        return []


async def fetch_all_congressional_trades(
    year: Optional[int] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch trades from House (Senate not available via automation).

    Senate eFD blocks server IPs, so only House data is available
    through bulk XML downloads.
    """
    if year is None:
        year = date.today().year

    trades = await fetch_house_trades(year=year, limit=limit)

    logger.info(f"Congressional trades total: {len(trades)} (House only — Senate eFD blocks automated access)")
    return trades
