"""Data refresh pipeline — fetch from live APIs and store in DynamoDB.

Each refresh function:
  1. Calls the existing fetch function from edgar/, congress/, economic/
  2. Transforms the result into DynamoDB items (PK/SK/data/ttl)
  3. Batch-writes to DynamoDB
  4. Updates META timestamps

Triggered by:
  - EventBridge daily/weekly cron -> Lambda handler -> refresh_all()
  - CLI: civicledger refresh all

TTLs:
  - Fundamentals: 7 days (slow-moving quarterly data)
  - Earnings/Insider/Material events: 30 days
  - Congressional trades: 90 days
  - Economic events: 30 days
  - Institutional holdings: 7 days
"""

import time
from datetime import date, timedelta
from typing import Any, Dict, List

from loguru import logger

from civicledger import storage

# TTL durations in seconds
TTL_7_DAYS = 7 * 24 * 60 * 60
TTL_30_DAYS = 30 * 24 * 60 * 60
TTL_90_DAYS = 90 * 24 * 60 * 60


# ---------------------------------------------------------------------------
# 1. Fundamentals (EDGAR XBRL)
# ---------------------------------------------------------------------------

async def refresh_fundamentals() -> int:
    """Fetch fundamentals for all tickers and store each in DynamoDB.

    PK: FUND#<TICKER>  SK: LATEST  TTL: 7 days

    Returns the number of tickers stored.
    """
    from civicledger.edgar.fundamentals import fetch_fundamentals

    logger.info("Refreshing fundamentals...")
    data = await fetch_fundamentals()

    if not data:
        logger.warning("No fundamentals data returned from EDGAR")
        return 0

    # Build batch items
    items = []
    for ticker, metrics in data.items():
        items.append(storage.build_item(
            pk=f"FUND#{ticker}",
            sk="LATEST",
            data=metrics,
            ttl_seconds=TTL_7_DAYS,
            gsi1pk="FUND",
            gsi1sk=ticker,
        ))

    # Batch write
    await storage.batch_write(items)

    # Update metadata
    await storage.put_meta("fundamentals", {
        "count": len(data),
        "sample_tickers": sorted(data.keys())[:10],
    })

    logger.info(f"Stored fundamentals for {len(data)} tickers")
    return len(data)


# ---------------------------------------------------------------------------
# 2. Earnings Calendar (EDGAR 8-K Item 2.02)
# ---------------------------------------------------------------------------

async def refresh_earnings(from_date: str, to_date: str) -> int:
    """Fetch earnings announcements and store in DynamoDB.

    PK: EARNINGS  SK: <filing_date>#<ticker>  TTL: 30 days

    Returns the number of announcements stored.
    """
    from civicledger.edgar.earnings import fetch_earnings

    logger.info(f"Refreshing earnings {from_date} to {to_date}...")
    data = await fetch_earnings(from_date, to_date)

    if not data:
        logger.info("No earnings announcements found")
        return 0

    items = []
    for entry in data:
        filing_date = entry.get("filing_date", "unknown")
        ticker = entry.get("ticker", "UNKNOWN")
        items.append(storage.build_item(
            pk="EARNINGS",
            sk=f"{filing_date}#{ticker}",
            data=entry,
            ttl_seconds=TTL_30_DAYS,
            gsi1pk="EARNINGS",
            gsi1sk=filing_date,
        ))

    await storage.batch_write(items)
    await storage.put_meta("earnings", {
        "count": len(data),
        "from_date": from_date,
        "to_date": to_date,
    })

    logger.info(f"Stored {len(data)} earnings announcements")
    return len(data)


# ---------------------------------------------------------------------------
# 3. Insider Trades (EDGAR Form 4)
# ---------------------------------------------------------------------------

async def refresh_insider_trades(from_date: str, to_date: str) -> int:
    """Fetch insider trades and store in DynamoDB.

    PK: INSIDER  SK: <filing_date>#<company>#<insider_name>  TTL: 30 days

    Returns the number of trades stored.
    """
    from civicledger.edgar.insider_trades import fetch_recent_insider_trades

    logger.info(f"Refreshing insider trades {from_date} to {to_date}...")
    data = await fetch_recent_insider_trades(from_date, to_date)

    if not data:
        logger.info("No insider trades found")
        return 0

    items = []
    for entry in data:
        filing_date = entry.get("filing_date", "unknown")
        company = (entry.get("company") or "unknown").replace("#", "_")[:50]
        insider = (entry.get("insider_name") or "unknown").replace("#", "_")[:50]
        items.append(storage.build_item(
            pk="INSIDER",
            sk=f"{filing_date}#{company}#{insider}",
            data=entry,
            ttl_seconds=TTL_30_DAYS,
            gsi1pk="INSIDER",
            gsi1sk=filing_date,
        ))

    await storage.batch_write(items)
    await storage.put_meta("insider_trades", {
        "count": len(data),
        "from_date": from_date,
        "to_date": to_date,
    })

    logger.info(f"Stored {len(data)} insider trades")
    return len(data)


# ---------------------------------------------------------------------------
# 4. Material Events (EDGAR 8-K)
# ---------------------------------------------------------------------------

async def refresh_material_events(from_date: str, to_date: str) -> int:
    """Fetch material corporate events and store in DynamoDB.

    PK: MATERIAL  SK: <filing_date>#<cik>#<items_joined>  TTL: 30 days

    Returns the number of events stored.
    """
    from civicledger.edgar.material_events import fetch_material_events

    logger.info(f"Refreshing material events {from_date} to {to_date}...")
    data = await fetch_material_events(from_date, to_date)

    if not data:
        logger.info("No material events found")
        return 0

    items = []
    for entry in data:
        filing_date = entry.get("filing_date", "unknown")
        cik = str(entry.get("cik", "unknown"))
        event_items = ",".join(entry.get("items", []))
        items.append(storage.build_item(
            pk="MATERIAL",
            sk=f"{filing_date}#{cik}#{event_items}",
            data=entry,
            ttl_seconds=TTL_30_DAYS,
            gsi1pk="MATERIAL",
            gsi1sk=filing_date,
        ))

    await storage.batch_write(items)
    await storage.put_meta("material_events", {
        "count": len(data),
        "from_date": from_date,
        "to_date": to_date,
    })

    logger.info(f"Stored {len(data)} material events")
    return len(data)


# ---------------------------------------------------------------------------
# 5. Congressional Trades
# ---------------------------------------------------------------------------

async def refresh_congressional_trades(year: int) -> int:
    """Fetch congressional trades and store in DynamoDB.

    PK: CONGRESS  SK: <year>#<politician>#<disclosure_date>  TTL: 90 days

    Returns the number of trades stored.
    """
    from civicledger.congress.trades import fetch_all_congressional_trades

    logger.info(f"Refreshing congressional trades for {year}...")
    data = await fetch_all_congressional_trades(year=year)

    if not data:
        logger.info(f"No congressional trades found for {year}")
        return 0

    items = []
    for entry in data:
        politician = (entry.get("politician") or "unknown").replace("#", "_")[:50]
        disc_date = entry.get("disclosure_date", "unknown")
        chamber = entry.get("chamber", "unknown")
        items.append(storage.build_item(
            pk="CONGRESS",
            sk=f"{year}#{politician}#{disc_date}#{chamber}",
            data=entry,
            ttl_seconds=TTL_90_DAYS,
            gsi1pk="CONGRESS",
            gsi1sk=f"{year}#{disc_date}",
        ))

    await storage.batch_write(items)
    await storage.put_meta("congressional_trades", {
        "count": len(data),
        "year": year,
    })

    logger.info(f"Stored {len(data)} congressional trades")
    return len(data)


# ---------------------------------------------------------------------------
# 6. Economic Events (FRED)
# ---------------------------------------------------------------------------

async def refresh_economic_events(from_date: str, to_date: str) -> int:
    """Fetch FRED economic events and store in DynamoDB.

    PK: ECONOMIC  SK: <date>#<event_name>  TTL: 30 days

    Returns the number of events stored.
    """
    from civicledger.economic.fred import fetch_economic_events

    logger.info(f"Refreshing economic events {from_date} to {to_date}...")
    data = await fetch_economic_events(from_date, to_date)

    if not data:
        logger.info("No economic events found")
        return 0

    items = []
    for entry in data:
        event_date = entry.get("date", "unknown")
        event_name = (entry.get("name") or "unknown").replace("#", "_")[:60]
        items.append(storage.build_item(
            pk="ECONOMIC",
            sk=f"{event_date}#{event_name}",
            data=entry,
            ttl_seconds=TTL_30_DAYS,
            gsi1pk="ECONOMIC",
            gsi1sk=event_date,
        ))

    await storage.batch_write(items)
    await storage.put_meta("economic_events", {
        "count": len(data),
        "from_date": from_date,
        "to_date": to_date,
    })

    logger.info(f"Stored {len(data)} economic events")
    return len(data)


# ---------------------------------------------------------------------------
# 7. Institutional Holdings (13F)
# ---------------------------------------------------------------------------

async def refresh_institutional_holdings() -> int:
    """Fetch 13F holdings for top institutions and store in DynamoDB.

    PK: INST#<ManagerNameNoSpaces>  SK: LATEST  TTL: 7 days

    Returns the number of institutions stored.
    """
    from civicledger.edgar.institutional import fetch_holdings, TOP_INSTITUTIONS

    logger.info("Refreshing institutional holdings...")
    count = 0

    for name, cik in TOP_INSTITUTIONS:
        try:
            data = await fetch_holdings(cik, limit=100)
            if "error" in data:
                logger.debug(f"Skipping {name}: {data['error']}")
                continue

            # Use a sanitized manager name as part of the PK
            safe_name = name.replace(" ", "").replace("#", "")
            await storage.put_item(
                pk=f"INST#{safe_name}",
                sk="LATEST",
                data=data,
                ttl_seconds=TTL_7_DAYS,
                gsi1pk="INST",
                gsi1sk=safe_name,
            )
            count += 1
            logger.debug(f"Stored holdings for {name}")

        except Exception as e:
            logger.warning(f"Failed to refresh holdings for {name}: {e}")

    await storage.put_meta("institutional_holdings", {
        "count": count,
        "institutions": [name for name, _ in TOP_INSTITUTIONS],
    })

    logger.info(f"Stored holdings for {count} institutions")
    return count


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

async def refresh_all(from_date: str | None = None,
                      to_date: str | None = None,
                      year: int | None = None) -> Dict[str, int]:
    """Run all refresh functions with sensible defaults.

    Returns a dict of {source: count} for each refresh.
    """
    today = date.today()
    if from_date is None:
        from_date = (today - timedelta(days=7)).isoformat()
    if to_date is None:
        to_date = today.isoformat()
    if year is None:
        year = today.year

    logger.info(f"=== Full refresh: {from_date} to {to_date}, year={year} ===")
    start = time.time()

    results: Dict[str, int] = {}

    # Run fundamentals first (heaviest, ~20 API calls)
    try:
        results["fundamentals"] = await refresh_fundamentals()
    except Exception as e:
        logger.error(f"Fundamentals refresh failed: {e}")
        results["fundamentals"] = 0

    # Run the rest
    refresh_tasks = [
        ("earnings", refresh_earnings(from_date, to_date)),
        ("insider_trades", refresh_insider_trades(from_date, to_date)),
        ("material_events", refresh_material_events(from_date, to_date)),
        ("congressional_trades", refresh_congressional_trades(year)),
        ("economic_events", refresh_economic_events(from_date, to_date)),
        ("institutional_holdings", refresh_institutional_holdings()),
    ]

    for name, coro in refresh_tasks:
        try:
            results[name] = await coro
        except Exception as e:
            logger.error(f"{name} refresh failed: {e}")
            results[name] = 0

    elapsed = time.time() - start
    total = sum(results.values())

    await storage.put_meta("all", {
        "results": results,
        "total_items": total,
        "elapsed_seconds": round(elapsed, 1),
        "from_date": from_date,
        "to_date": to_date,
        "year": year,
    })

    logger.info(f"=== Full refresh complete: {total} items in {elapsed:.1f}s ===")
    logger.info(f"Results: {results}")
    return results
