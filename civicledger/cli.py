"""CivicLedger CLI — refresh data from public sources.

Usage:
    civicledger refresh fundamentals
    civicledger refresh earnings --from 2026-03-01 --to 2026-03-31
    civicledger refresh insider-trades --from 2026-03-01 --to 2026-03-07
    civicledger refresh congress --year 2026
    civicledger refresh events --from 2026-03-01 --to 2026-03-31
    civicledger refresh material-events --from 2026-03-01 --to 2026-03-31
    civicledger refresh institutional
    civicledger refresh all
    civicledger serve --port 8080
    civicledger mcp                       # start MCP server (stdio)
    civicledger mcp --transport sse       # start MCP server (SSE)

When DynamoDB is configured (CIVICLEDGER_DYNAMODB_TABLE), refresh commands
store data in DynamoDB. Otherwise they just print results to stdout.
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta

from loguru import logger


def _run(coro):
    """Run an async function."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Refresh commands — DynamoDB-backed (new)
# ---------------------------------------------------------------------------

async def _refresh_fundamentals():
    """Refresh fundamentals and store in DynamoDB."""
    try:
        from civicledger.refresh import refresh_fundamentals
        count = await refresh_fundamentals()
        print(f"Stored fundamentals for {count} tickers in DynamoDB")
    except Exception as e:
        logger.warning(f"DynamoDB refresh failed, running live fetch only: {e}")
        from civicledger.edgar.fundamentals import fetch_fundamentals
        data = await fetch_fundamentals()
        print(f"Fetched fundamentals for {len(data)} tickers (not stored — DynamoDB unavailable)")
        for ticker in list(data.keys())[:5]:
            metrics = data[ticker]
            print(f"  {ticker}: revenue={metrics.get('revenue')}, margin={metrics.get('profit_margin')}")


async def _refresh_earnings(from_date: str, to_date: str):
    try:
        from civicledger.refresh import refresh_earnings
        count = await refresh_earnings(from_date, to_date)
        print(f"Stored {count} earnings announcements in DynamoDB")
    except Exception as e:
        logger.warning(f"DynamoDB refresh failed: {e}")
        from civicledger.edgar.earnings import fetch_earnings
        data = await fetch_earnings(from_date, to_date)
        print(f"Fetched {len(data)} earnings announcements (not stored)")
        for entry in data[:10]:
            print(f"  {entry['filing_date']} - {entry['ticker']:>6} - {entry['company']}")


async def _refresh_insider_trades(from_date: str, to_date: str):
    try:
        from civicledger.refresh import refresh_insider_trades
        count = await refresh_insider_trades(from_date, to_date)
        print(f"Stored {count} insider trade filings in DynamoDB")
    except Exception as e:
        logger.warning(f"DynamoDB refresh failed: {e}")
        from civicledger.edgar.insider_trades import fetch_recent_insider_trades
        data = await fetch_recent_insider_trades(from_date, to_date)
        print(f"Fetched {len(data)} insider trade filings (not stored)")
        for t in data[:10]:
            print(f"  {t['filing_date']} - {t.get('ticker', '?'):>6} - {t.get('insider_name', '?')}")


async def _refresh_congress(year: int):
    try:
        from civicledger.refresh import refresh_congressional_trades
        count = await refresh_congressional_trades(year)
        print(f"Stored {count} congressional trades in DynamoDB")
    except Exception as e:
        logger.warning(f"DynamoDB refresh failed: {e}")
        from civicledger.congress.trades import fetch_all_congressional_trades
        data = await fetch_all_congressional_trades(year=year)
        print(f"Fetched {len(data)} congressional trades (not stored)")
        for t in data[:10]:
            print(f"  {t['disclosure_date']} - {t['politician']} ({t['chamber']})")


async def _refresh_events(from_date: str, to_date: str):
    try:
        from civicledger.refresh import refresh_economic_events
        count = await refresh_economic_events(from_date, to_date)
        print(f"Stored {count} economic events in DynamoDB")
    except Exception as e:
        logger.warning(f"DynamoDB refresh failed: {e}")
        from civicledger.economic.fred import fetch_economic_events
        data = await fetch_economic_events(from_date, to_date)
        print(f"Fetched {len(data)} economic events (not stored)")
        for entry in data[:10]:
            print(f"  {entry['date']} - {entry['name']} ({entry['impact']})")


async def _refresh_material_events(from_date: str, to_date: str):
    try:
        from civicledger.refresh import refresh_material_events
        count = await refresh_material_events(from_date, to_date)
        print(f"Stored {count} material events in DynamoDB")
    except Exception as e:
        logger.warning(f"DynamoDB refresh failed: {e}")
        from civicledger.edgar.material_events import fetch_material_events
        data = await fetch_material_events(from_date, to_date)
        print(f"Fetched {len(data)} material events (not stored)")
        for entry in data[:10]:
            labels = ", ".join(entry.get("item_labels", []))
            print(f"  {entry['filing_date']} - {entry.get('ticker', '?'):>6} - {labels}")


async def _refresh_institutional():
    try:
        from civicledger.refresh import refresh_institutional_holdings
        count = await refresh_institutional_holdings()
        print(f"Stored holdings for {count} institutions in DynamoDB")
    except Exception as e:
        logger.warning(f"DynamoDB refresh failed: {e}")
        from civicledger.edgar.institutional import fetch_top_institutions_summary
        data = await fetch_top_institutions_summary()
        print(f"Fetched {len(data)} institutions (not stored)")
        for inst in data:
            print(f"  {inst['manager_name']}: ${inst.get('total_value_millions', '?')}M")


async def _refresh_all(from_date: str, to_date: str, year: int):
    print("=== Refreshing all data sources ===\n")

    try:
        from civicledger.refresh import refresh_all
        results = await refresh_all(from_date, to_date, year)
        print("\n=== Refresh Results ===")
        for source, count in results.items():
            print(f"  {source}: {count} items")
        total = sum(results.values())
        print(f"\nTotal: {total} items stored in DynamoDB")
    except Exception as e:
        logger.warning(f"DynamoDB refresh_all failed ({e}), running individual fetches...")
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
        print("--- Institutional Holdings (13F) ---")
        await _refresh_institutional()
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
        choices=["fundamentals", "earnings", "insider-trades", "congress", "events", "material-events", "institutional", "all"],
        help="Data source to refresh",
    )
    refresh_parser.add_argument("--from", dest="from_date", default=None, help="Start date (YYYY-MM-DD)")
    refresh_parser.add_argument("--to", dest="to_date", default=None, help="End date (YYYY-MM-DD)")
    refresh_parser.add_argument("--year", type=int, default=None, help="Year for congressional trades")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start FastAPI server")
    serve_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to")
    serve_parser.add_argument("--port", type=int, default=8080, help="Port to bind to")

    # mcp
    mcp_parser = subparsers.add_parser("mcp", help="Start MCP (Model Context Protocol) server")
    mcp_parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport protocol: stdio (local/Claude Desktop) or sse (remote/HTTP)",
    )
    mcp_parser.add_argument("--host", default="0.0.0.0", help="Host to bind to (SSE only)")
    mcp_parser.add_argument("--port", type=int, default=8080, help="Port to bind to (SSE only)")

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
        elif args.source == "institutional":
            _run(_refresh_institutional())
        elif args.source == "all":
            _run(_refresh_all(from_date, to_date, year))

    elif args.command == "serve":
        from civicledger.api.server import create_app
        import uvicorn
        app = create_app()
        uvicorn.run(app, host=args.host, port=args.port)

    elif args.command == "mcp":
        from civicledger.mcp_server import run_server
        run_server(transport=args.transport, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
