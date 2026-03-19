"""Congressional stock trades from Senate eFD and House clerk disclosures.

Senate: https://efdsearch.senate.gov/search/
House: https://disclosures-clerk.house.gov/FinancialDisclosure

All data is public under the STOCK Act (2012). These are Periodic
Transaction Reports (PTRs) that members of Congress must file within
45 days of a trade.

Note: Senate disclosures require web scraping (ASPX form).
House disclosures provide annual ZIP files with XML data.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional

import httpx
from bs4 import BeautifulSoup
from loguru import logger

from civicledger.config import get_settings

SENATE_SEARCH_URL = "https://efdsearch.senate.gov/search/"
HOUSE_BASE_URL = "https://disclosures-clerk.house.gov"
HOUSE_SEARCH_URL = f"{HOUSE_BASE_URL}/FinancialDisclosure/ViewSearch"


async def fetch_senate_trades(
    year: Optional[int] = None,
    senator: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Fetch periodic transaction reports from Senate eFD.

    Scrapes the Senate Electronic Financial Disclosures search form.
    Returns list of {politician, party, state, asset_description,
    transaction_type, transaction_date, amount_range, disclosure_date}.
    """
    from datetime import date

    if year is None:
        year = date.today().year

    settings = get_settings()
    headers = {
        "User-Agent": settings.edgar_identity,
        "Accept": "text/html,application/xhtml+xml",
    }

    trades: List[Dict[str, Any]] = []

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # First GET to establish session and get CSRF token
            resp = await client.get(SENATE_SEARCH_URL, headers=headers)
            soup = BeautifulSoup(resp.text, "html.parser")

            # Extract any hidden form fields (CSRF, viewstate, etc.)
            form_data = {}
            for inp in soup.find_all("input", {"type": "hidden"}):
                name = inp.get("name")
                value = inp.get("value", "")
                if name:
                    form_data[name] = value

            # Submit search for Periodic Transaction Reports
            form_data.update({
                "report_type": "ptr",  # Periodic Transaction Reports
                "submitted_start_date": f"01/01/{year}",
                "submitted_end_date": f"12/31/{year}",
            })
            if senator:
                form_data["last_name"] = senator

            resp = await client.post(
                SENATE_SEARCH_URL,
                data=form_data,
                headers=headers,
            )

            if resp.status_code != 200:
                logger.warning(f"Senate eFD search returned {resp.status_code}")
                return []

            # Parse results table
            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table", class_="table")
            if not table:
                logger.info("No Senate PTR results found")
                return []

            rows = table.find_all("tr")[1:]  # Skip header
            for row in rows[:limit]:
                cols = row.find_all("td")
                if len(cols) < 4:
                    continue

                name_parts = cols[0].get_text(strip=True).split(",")
                last_name = name_parts[0].strip() if name_parts else ""
                first_name = name_parts[1].strip() if len(name_parts) > 1 else ""

                link = cols[3].find("a")
                report_url = link["href"] if link else None

                trades.append({
                    "politician": f"{first_name} {last_name}".strip(),
                    "chamber": "senate",
                    "party": None,  # Not in search results
                    "state": None,
                    "disclosure_date": cols[1].get_text(strip=True),
                    "transaction_date": None,  # Need to parse individual report
                    "asset_description": None,
                    "ticker": None,
                    "transaction_type": None,
                    "amount_range": None,
                    "source_url": f"https://efdsearch.senate.gov{report_url}" if report_url else None,
                })

    except Exception as e:
        logger.warning(f"Senate eFD scraping failed: {e}")

    logger.info(f"Senate trades: {len(trades)} PTRs for {year}")
    return trades


async def fetch_house_trades(
    year: Optional[int] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Fetch periodic transaction reports from House clerk disclosures.

    The House provides a search interface and individual PTR documents.
    Returns list of {politician, party, state, disclosure_date, source_url}.
    """
    from datetime import date

    if year is None:
        year = date.today().year

    settings = get_settings()
    headers = {"User-Agent": settings.edgar_identity}

    trades: List[Dict[str, Any]] = []

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            # Search for PTRs
            resp = await client.get(
                HOUSE_SEARCH_URL,
                params={
                    "FilingYear": str(year),
                    "State": "",
                    "District": "",
                    "LastName": "",
                    "FilingType": "ptr",  # Periodic Transaction Reports
                },
                headers=headers,
            )

            if resp.status_code != 200:
                logger.warning(f"House clerk search returned {resp.status_code}")
                return []

            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table")
            if not table:
                logger.info("No House PTR results found")
                return []

            rows = table.find_all("tr")[1:]  # Skip header
            for row in rows[:limit]:
                cols = row.find_all("td")
                if len(cols) < 5:
                    continue

                name = cols[0].get_text(strip=True)
                office = cols[1].get_text(strip=True)
                filing_year = cols[2].get_text(strip=True)
                filing_date = cols[3].get_text(strip=True)

                link = cols[0].find("a")
                doc_url = f"{HOUSE_BASE_URL}{link['href']}" if link and link.get("href") else None

                # Parse state from office (e.g., "CA05" → "CA")
                state_match = re.match(r"([A-Z]{2})", office)
                state = state_match.group(1) if state_match else None

                trades.append({
                    "politician": name,
                    "chamber": "house",
                    "party": None,
                    "state": state,
                    "disclosure_date": filing_date,
                    "transaction_date": None,
                    "asset_description": None,
                    "ticker": None,
                    "transaction_type": None,
                    "amount_range": None,
                    "source_url": doc_url,
                })

    except Exception as e:
        logger.warning(f"House clerk scraping failed: {e}")

    logger.info(f"House trades: {len(trades)} PTRs for {year}")
    return trades


async def fetch_all_congressional_trades(
    year: Optional[int] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """Fetch trades from both Senate and House, merged and sorted by date."""
    senate, house = await asyncio.gather(
        fetch_senate_trades(year=year, limit=limit),
        fetch_house_trades(year=year, limit=limit),
        return_exceptions=True,
    )

    all_trades = []
    if isinstance(senate, list):
        all_trades.extend(senate)
    if isinstance(house, list):
        all_trades.extend(house)

    all_trades.sort(
        key=lambda t: t.get("disclosure_date") or t.get("transaction_date") or "",
        reverse=True,
    )

    logger.info(f"Congressional trades total: {len(all_trades)} ({len(senate) if isinstance(senate, list) else 0} Senate + {len(house) if isinstance(house, list) else 0} House)")
    return all_trades
