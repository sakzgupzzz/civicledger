"""CivicLedger CLI — refresh data from public sources.

Usage:
    civicledger refresh fundamentals
    civicledger refresh earnings --from 2026-03-01 --to 2026-03-31
    civicledger refresh insider-trades --from 2026-03-01 --to 2026-03-07
    civicledger refresh congress --year 2026
    civicledger refresh events --from 2026-03-01 --to 2026-03-31
    civicledger refresh all
    civicledger serve --port 8080
"""

import argparse
import asyncio
import json
import sys
from datetime import date, timedelta

from loguru import logger


def _run(coro):
    """Run an async function."""
    return asyncio.run(coro)


async def _refresh_fundamentals():
    from civicledger.edgar.fundamentals import fetch_fundamentals
    data = await fetch_fundamentals()
    print(f"Fetched fundamentals for {len(data)} tickers")
    # Show sample
    for ticker in list(data.keys())[:5]:
        metrics = data[ticker]
        print(f"  {ticker}: revenue={metrics.get('revenue')}, margin={metrics.get('profit_margin')}")


async def _refresh_earnings(from_date: str, to_date: str):
    from civicledger.edgar.earnings import fetch_earnings
    data = await fetch_earnings(from_date, to_date)
    print(f"Fetched {len(data)} earnings announcements")
    for e in data[:10]:
        print(f"  {e['filing_date']} - {e['ticker']:>6} - {e['company']}")


async def _refresh_insider_trades(from_date: str, to_date: str):
    from civicledger.edgar.insider_trades import fetch_recent_insider_trades
    data = await fetch_recent_insider_trades(from_date, to_date)
    print(f"Fetched {len(data)} insider trade filings")
    for t in data[:10]:
        print(f"  {t['filing_date']} - {t['ticker']:>6} - {t['insider_name']}")


async def _refresh_congress(year: int):
    from civicledger.congress.trades import fetch_all_congressional_trades
    data = await fetch_all_congressional_trades(year=year)
    print(f"Fetched {len(data)} congressional trades")
    for t in data[:10]:
        print(f"  {t['disclosure_date']} - {t['politician']} ({t['chamber']})")


async def _refresh_events(from_date: str, to_date: str):
    from civicledger.economic.fred import fetch_economic_events
    data = await fetch_economic_events(from_date, to_date)
    print(f"Fetched {len(data)} economic events")
    for e in data[:10]:
        print(f"  {e['date']} - {e['name']} ({e['impact']})")


async def _refresh_material_events(from_date: str, to_date: str):
    from civicledger.edgar.material_events import fetch_material_events
    data = await fetch_material_events(from_date, to_date)
    print(f"Fetched {len(data)} material events")
    for e in data[:10]:
        labels = ", ".join(e.get("item_labels", []))
        print(f"  {e['filing_date']} - {e.get('ticker', '?'):>6} - {labels}")


async def _refresh_all(from_date: str, to_date: str, year: int):
    print("=== Refreshing all data sources ===\n")

    print("--- Fundamentals (EDGAR XBRL) ---")
    await _refresh_fundamentals()
    print()

    print("--- Earnings Calendar (EDGAR 8-K) ---")
    await _refresh_earnings(from_date, to_date)
    print()

    print("--- Insider Trades (EDGAR Form 4) ---")
    await _refresh_insider_trades(from_date, to_date)
    print()

    print("--- Congressional Trades ---")
    await _refresh_congress(year)
    print()

    print("--- Economic Events (FRED) ---")
    await _refresh_events(from_date, to_date)
    print()

    print("--- Material Events (EDGAR 8-K) ---")
    await _refresh_material_events(from_date, to_date)
    print()

    print("=== All refreshes complete ===")


def main():
    parser = argparse.ArgumentParser(
        prog="civicledger",
        description="US financial intelligence from public domain sources",
    )
    subparsers = parser.add_subparsers(dest="command")

    # refresh
    refresh_parser = subparsers.add_parser("refresh", help="Refresh data from public sources")
    refresh_parser.add_argument(
        "source",
        choices=["fundamentals", "earnings", "insider-trades", "congress", "events", "material-events", "all"],
        help="Data source to refresh",
    )
    refresh_parser.add_argument("--from", dest="from_date", default=None, help="Start date (YYYY-MM-DD)")
    refresh_parser.add_argument("--to", dest="to_date", default=None, help="End date (YYYY-MM-DD)")
    refresh_parser.add_argument("--year", type=int, default=None, help="Year for congressional trades")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start FastAPI server")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    serve_parser.add_argument("--port", type=int, default=8080, help="Port to bind to")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    today = date.today()
    from_date = args.from_date if hasattr(args, "from_date") and args.from_date else (today - timedelta(days=7)).isoformat()
    to_date = args.to_date if hasattr(args, "to_date") and args.to_date else today.isoformat()
    year = args.year if hasattr(args, "year") and args.year else today.year

    if args.command == "refresh":
        if args.source == "fundamentals":
            _run(_refresh_fundamentals())
        elif args.source == "earnings":
            _run(_refresh_earnings(from_date, to_date))
        elif args.source == "insider-trades":
            _run(_refresh_insider_trades(from_date, to_date))
        elif args.source == "congress":
            _run(_refresh_congress(year))
        elif args.source == "events":
            _run(_refresh_events(from_date, to_date))
        elif args.source == "material-events":
            _run(_refresh_material_events(from_date, to_date))
        elif args.source == "all":
            _run(_refresh_all(from_date, to_date, year))

    elif args.command == "serve":
        from civicledger.api.server import create_app
        import uvicorn
        app = create_app()
        uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
