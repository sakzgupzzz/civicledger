"""CivicLedger MCP Server — expose US financial data as MCP tools.

Wraps all CivicLedger data sources as MCP tools. When DynamoDB is populated
(via the refresh pipeline), tools read from the database for instant responses.
Falls back to live API fetches if DynamoDB is empty or unavailable.

Supports:
  - stdio transport (local use with Claude Desktop)
  - sse transport (remote hosting)

Usage:
  civicledger mcp                       # stdio (default)
  civicledger mcp --transport sse       # SSE on port 8080
  civicledger mcp --transport sse --port 9090

All data sourced from US government APIs. Public domain.
"""

import json
from typing import Any

from loguru import logger
from mcp.server.fastmcp import FastMCP

from civicledger.economic.fred import FredApiKeyMissing
from civicledger.validation import (
    ValidationError,
    normalize_8k_item,
    normalize_date_range,
    normalize_limit,
    normalize_ticker,
    normalize_year,
)

mcp = FastMCP(
    "CivicLedger",
    instructions=(
        "US financial intelligence from public domain sources. "
        "SEC EDGAR filings (fundamentals, earnings, insider trades, 13F holdings, material events), "
        "FRED economic calendar, and congressional stock trades. "
        "All data is from US government APIs. No private data providers."
    ),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_result(data: Any, summary: str | None = None) -> str:
    """Format tool output as JSON text with an optional summary header."""
    if summary:
        return f"{summary}\n\n{json.dumps(data, indent=2, default=str)}"
    return json.dumps(data, indent=2, default=str)


def _tool_error(name: str, e: Exception) -> str:
    """Log the full traceback server-side; return a clean message to the model."""
    logger.opt(exception=True).error(f"{name} failed: {e}")
    return f"Error in {name}: {e}"


async def _try_dynamo_first(fetch_from_dynamo, fetch_live, source_name: str):
    """Try DynamoDB first, fall back to live fetch.

    Args:
        fetch_from_dynamo: async callable that returns data from DynamoDB (or None/empty)
        fetch_live: async callable that returns data from live API
        source_name: human-readable name for logging

    Returns the data from whichever source succeeds.
    """
    try:
        data = await fetch_from_dynamo()
        if data:
            logger.debug(f"{source_name}: served from DynamoDB ({len(data) if isinstance(data, (list, dict)) else '?'} items)")
            return data
        logger.debug(f"{source_name}: DynamoDB empty, falling back to live fetch")
    except Exception as e:
        logger.debug(f"{source_name}: DynamoDB unavailable ({e}), falling back to live fetch")

    return await fetch_live()


# ---------------------------------------------------------------------------
# Tool 1: Fundamentals
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_fundamentals(ticker: str | None = None) -> str:
    """Fetch quarterly financial fundamentals from SEC EDGAR XBRL filings.

    Returns metrics like revenue, net income, profit margin, gross margin,
    operating margin, return on equity, return on assets, debt-to-equity,
    current ratio, EPS, revenue growth, and earnings growth.

    Source: SEC EDGAR XBRL Frames API (public domain, no API key needed).

    Args:
        ticker: Optional stock ticker (e.g., "AAPL"). If provided, returns
                data for just that ticker. If omitted, returns a summary
                of all ~4,000 tickers (ticker list + count only, to avoid
                overwhelming output).
    """
    try:
        if ticker:
            ticker_upper = normalize_ticker(ticker)

            # Try DynamoDB first for single ticker
            async def _from_dynamo():
                from civicledger import storage
                return await storage.get_item(f"FUND#{ticker_upper}", "LATEST")

            async def _from_live():
                from civicledger.edgar.fundamentals import fetch_fundamentals_for_ticker
                return await fetch_fundamentals_for_ticker(ticker_upper)

            data = await _try_dynamo_first(_from_dynamo, _from_live, f"fundamentals/{ticker_upper}")

            if data:
                return _format_result(
                    {ticker_upper: data},
                    f"Fundamentals for {ticker_upper}",
                )
            return f"No fundamentals data found for ticker '{ticker_upper}'. This ticker may not file with SEC EDGAR."

        # No ticker — return summary
        async def _summary_from_dynamo():
            from civicledger import storage
            meta = await storage.get_meta("fundamentals")
            if meta and meta.get("count", 0) > 0:
                # Fetch a small sample from GSI
                sample_items = await storage.query_gsi("FUND", limit=20)
                sample_data = {}
                for item in sample_items:
                    sk = item.pop("_sk", "")
                    # SK is the ticker itself in GSI1SK
                    sample_data[sk] = item
                return {
                    "total_tickers": meta["count"],
                    "sample_tickers": sorted(sample_data.keys())[:20],
                    "sample_data": sample_data,
                    "note": "Pass a specific ticker to get_fundamentals(ticker='AAPL') for full data.",
                    "last_refresh": meta.get("timestamp"),
                }
            return None

        async def _summary_from_live():
            from civicledger.edgar.fundamentals import fetch_fundamentals
            data = await fetch_fundamentals()
            if not data:
                return None
            sample_tickers = sorted(data.keys())[:20]
            sample = {t: data[t] for t in sample_tickers}
            return {
                "total_tickers": len(data),
                "sample_tickers": sample_tickers,
                "sample_data": sample,
                "note": "Pass a specific ticker to get_fundamentals(ticker='AAPL') for full data.",
            }

        data = await _try_dynamo_first(_summary_from_dynamo, _summary_from_live, "fundamentals/summary")

        if data:
            return _format_result(
                data,
                f"EDGAR XBRL fundamentals: {data.get('total_tickers', '?')} tickers available",
            )
        return "No fundamentals data available. The EDGAR XBRL API may be temporarily unavailable."

    except ValidationError as e:
        return f"Invalid input: {e}"
    except Exception as e:
        return _tool_error("get_fundamentals", e)


# ---------------------------------------------------------------------------
# Tool 2: Earnings Calendar
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_earnings_calendar(
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    """Fetch earnings announcements from SEC EDGAR 8-K Item 2.02 filings.

    Returns companies that reported earnings (filed "Results of Operations")
    in the given date range. Each result includes ticker, company name,
    filing date, and CIK number.

    Source: SEC EDGAR EFTS search (public domain, no API key needed).

    Args:
        from_date: Start date in YYYY-MM-DD format. Defaults to 7 days ago.
        to_date: End date in YYYY-MM-DD format. Defaults to today.
    """
    try:
        fd, td = normalize_date_range(from_date, to_date)

        async def _from_dynamo():
            from civicledger import storage
            # Query by date prefix to get items in range
            items = await storage.query("EARNINGS", sk_prefix=fd[:7])
            # Filter to exact date range and strip internal keys
            filtered = []
            for item in items:
                item.pop("_sk", None)
                item_date = item.get("filing_date", "")
                if fd <= item_date <= td:
                    filtered.append(item)
            return filtered if filtered else None

        async def _from_live():
            from civicledger.edgar.earnings import fetch_earnings
            return await fetch_earnings(fd, td)

        data = await _try_dynamo_first(_from_dynamo, _from_live, "earnings")

        if not data:
            return f"No earnings announcements found between {fd} and {td}."

        return _format_result(
            data,
            f"{len(data)} earnings announcements from {fd} to {td}",
        )

    except ValidationError as e:
        return f"Invalid input: {e}"
    except Exception as e:
        return _tool_error("get_earnings_calendar", e)


# ---------------------------------------------------------------------------
# Tool 3: Recent Insider Trades
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_insider_trades(
    from_date: str | None = None,
    to_date: str | None = None,
    ticker: str | None = None,
) -> str:
    """Fetch recent SEC Form 4 insider trade filings.

    Form 4 is filed within 2 business days when corporate officers, directors,
    or 10%+ owners buy or sell company stock. Returns filing metadata including
    ticker, company, insider name, and filing date.

    Source: SEC EDGAR (public domain, no API key needed).

    Args:
        from_date: Start date in YYYY-MM-DD format. Defaults to 7 days ago.
        to_date: End date in YYYY-MM-DD format. Defaults to today.
        ticker: Optional — filter to a specific stock ticker (e.g., "TSLA").
    """
    try:
        fd, td = normalize_date_range(from_date, to_date, max_span_days=92)
        tk = normalize_ticker(ticker) if ticker else None

        async def _from_dynamo():
            from civicledger import storage
            items = await storage.query("INSIDER", sk_prefix=fd[:7])
            filtered = []
            for item in items:
                item.pop("_sk", None)
                item_date = item.get("filing_date", "")
                if fd <= item_date <= td:
                    if tk and item.get("ticker") != tk:
                        continue
                    filtered.append(item)
            return filtered if filtered else None

        async def _from_live():
            from civicledger.edgar.insider_trades import fetch_recent_insider_trades
            return await fetch_recent_insider_trades(fd, td, ticker=tk)

        data = await _try_dynamo_first(_from_dynamo, _from_live, "insider_trades")

        if not data:
            filter_msg = f" for {tk}" if tk else ""
            return f"No insider trades found{filter_msg} between {fd} and {td}."

        return _format_result(
            data,
            f"{len(data)} insider trade filings from {fd} to {td}" + (f" (filtered to {tk})" if tk else ""),
        )

    except ValidationError as e:
        return f"Invalid input: {e}"
    except Exception as e:
        return _tool_error("get_insider_trades", e)


# ---------------------------------------------------------------------------
# Tool 4: Detailed Insider Trades for a Ticker
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_insider_trades_detailed(
    ticker: str,
    limit: int = 50,
) -> str:
    """Fetch detailed insider trade history for a specific stock ticker.

    Parses actual Form 4 XML filings for transaction details including shares
    traded, price per share, transaction type, and insider title. More detailed
    than get_insider_trades() but requires a specific ticker.

    Source: SEC EDGAR Form 4 filings (public domain, no API key needed).

    Args:
        ticker: Stock ticker symbol (e.g., "AAPL"). Required.
        limit: Maximum number of trades to return. Defaults to 50.
    """
    try:
        tk = normalize_ticker(ticker)
        lim = normalize_limit(limit, default=50, maximum=500)
        # Detailed trades are per-ticker and require parsing Form 4 XML.
        # Not practical to pre-fetch for all tickers, so this always goes live.
        from civicledger.edgar.insider_trades import fetch_insider_trades_detailed

        data = await fetch_insider_trades_detailed(tk, limit=lim)

        if not data:
            return f"No detailed insider trades found for {tk}. The company may not have recent Form 4 filings."

        return _format_result(
            data,
            f"{len(data)} insider transactions for {tk}",
        )

    except ValidationError as e:
        return f"Invalid input: {e}"
    except Exception as e:
        return _tool_error("get_insider_trades_detailed", e)


# ---------------------------------------------------------------------------
# Tool 5: Institutional Holdings (13F)
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_institutional_holdings(
    manager: str,
    limit: int = 100,
) -> str:
    """Fetch 13F institutional holdings for a hedge fund or asset manager.

    13F filings are required quarterly from institutional investment managers
    with $100M+ in assets. Shows their equity positions (stocks, ETFs).

    Source: SEC EDGAR 13F-HR filings (public domain, no API key needed).

    Args:
        manager: Manager name (e.g., "Berkshire Hathaway", "ARK Invest") or
                 CIK number (e.g., "0001067983"). Required.
        limit: Maximum number of holdings to return. Defaults to 100.
    """
    try:
        if not manager or not manager.strip():
            raise ValidationError("manager is required (a fund name or CIK).")
        manager = manager.strip()
        limit = normalize_limit(limit, default=100, maximum=1000)
        # Try DynamoDB for known institutions
        safe_name = manager.replace(" ", "").replace("#", "")

        async def _from_dynamo():
            from civicledger import storage
            return await storage.get_item(f"INST#{safe_name}", "LATEST")

        async def _from_live():
            from civicledger.edgar.institutional import fetch_holdings
            return await fetch_holdings(manager, limit=limit)

        data = await _try_dynamo_first(_from_dynamo, _from_live, f"institutional/{manager}")

        if not data:
            return f"No 13F holdings found for {manager}."

        if "error" in data:
            return f"Error: {data['error']}"

        # Apply limit to holdings if from DynamoDB (may have stored more)
        if "holdings" in data and len(data["holdings"]) > limit:
            data["holdings"] = data["holdings"][:limit]

        return _format_result(
            data,
            f"13F holdings for {data.get('manager_name', manager)} — "
            f"${data.get('total_value_millions', '?')}M across {data.get('holdings_count', '?')} positions",
        )

    except ValidationError as e:
        return f"Invalid input: {e}"
    except Exception as e:
        return _tool_error("get_institutional_holdings", e)


# ---------------------------------------------------------------------------
# Tool 6: Top Institutions Summary
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_top_institutions() -> str:
    """Fetch summary of top institutional investors and their 13F holdings.

    Returns portfolio size, number of holdings, and top 5 positions for
    major funds including Berkshire Hathaway, Bridgewater, Renaissance
    Technologies, Citadel, BlackRock, Vanguard, ARK Invest, Soros, and more.

    Note: This calls the SEC EDGAR API for each institution, so it may take
    30-60 seconds to complete if not cached.

    Source: SEC EDGAR 13F-HR filings (public domain, no API key needed).
    """
    try:
        async def _from_dynamo():
            from civicledger import storage
            from civicledger.edgar.institutional import TOP_INSTITUTIONS
            results = []
            for name, cik in TOP_INSTITUTIONS:
                safe_name = name.replace(" ", "").replace("#", "")
                data = await storage.get_item(f"INST#{safe_name}", "LATEST")
                if data and "error" not in data:
                    results.append({
                        "manager_name": name,
                        "manager_cik": cik,
                        "total_value_millions": data.get("total_value_millions"),
                        "holdings_count": data.get("holdings_count"),
                        "period": data.get("period"),
                        "top_holdings": data.get("holdings", [])[:5],
                    })
            return results if results else None

        async def _from_live():
            from civicledger.edgar.institutional import fetch_top_institutions_summary
            return await fetch_top_institutions_summary()

        data = await _try_dynamo_first(_from_dynamo, _from_live, "top_institutions")

        if not data:
            return "No institutional holdings data available."

        return _format_result(
            data,
            f"Top {len(data)} institutional investors (13F summary)",
        )

    except Exception as e:
        return _tool_error("get_top_institutions", e)


# ---------------------------------------------------------------------------
# Tool 7: Material Events (8-K)
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_material_events(
    from_date: str | None = None,
    to_date: str | None = None,
    item_filter: str | None = None,
) -> str:
    """Fetch material corporate events from SEC 8-K filings.

    8-K filings report significant events: earnings (2.02), mergers (2.01),
    officer changes (5.02), material agreements (1.01), restructuring (2.05),
    and more.

    Source: SEC EDGAR EFTS search (public domain, no API key needed).

    Args:
        from_date: Start date in YYYY-MM-DD format. Defaults to 7 days ago.
        to_date: End date in YYYY-MM-DD format. Defaults to today.
        item_filter: Optional 8-K item code to filter by. Common codes:
                     "1.01" (material agreement), "2.01" (acquisition),
                     "2.02" (earnings), "5.02" (officer change),
                     "7.01" (Reg FD disclosure), "8.01" (other events).
    """
    try:
        fd, td = normalize_date_range(from_date, to_date)
        item_filter = normalize_8k_item(item_filter)

        async def _from_dynamo():
            from civicledger import storage
            items = await storage.query("MATERIAL", sk_prefix=fd[:7])
            filtered = []
            for item in items:
                item.pop("_sk", None)
                item_date = item.get("filing_date", "")
                if fd <= item_date <= td:
                    if item_filter and item_filter not in item.get("items", []):
                        continue
                    filtered.append(item)
            return filtered if filtered else None

        async def _from_live():
            from civicledger.edgar.material_events import fetch_material_events
            return await fetch_material_events(fd, td, item_filter=item_filter)

        data = await _try_dynamo_first(_from_dynamo, _from_live, "material_events")

        if not data:
            filter_msg = f" (item {item_filter})" if item_filter else ""
            return f"No material events found{filter_msg} between {fd} and {td}."

        return _format_result(
            data,
            f"{len(data)} material events from {fd} to {td}" + (f" (filtered to item {item_filter})" if item_filter else ""),
        )

    except ValidationError as e:
        return f"Invalid input: {e}"
    except Exception as e:
        return _tool_error("get_material_events", e)


# ---------------------------------------------------------------------------
# Tool 8: Congressional Trades
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_congressional_trades(
    year: int | None = None,
    limit: int = 200,
    detailed: bool = False,
) -> str:
    """Fetch stock trades by members of the US House of Representatives.

    Under the STOCK Act (2012), members of Congress must disclose stock
    trades within 45 days. Returns Periodic Transaction Reports (PTRs)
    from the House clerk's bulk disclosures.

    Note: Senate trades are NOT available — the Senate eFD site
    (efdsearch.senate.gov) blocks automated access. This returns House data only.

    Source: House clerk disclosures (disclosures-clerk.house.gov). Public domain.

    Args:
        year: Year to search. Defaults to the current year.
        limit: Maximum results. Defaults to 200.
        detailed: When True, parse the PTR PDFs to return real per-transaction
                  detail (ticker, transaction type, amount range, dates).
                  Slower, since it downloads and parses individual filings.
    """
    try:
        yr = normalize_year(year)
        lim = normalize_limit(limit, default=200, maximum=1000)

        async def _from_dynamo():
            # Detailed results aren't pre-cached; force a live parse.
            if detailed:
                return None
            from civicledger import storage
            items = await storage.query("CONGRESS", sk_prefix=str(yr), limit=lim)
            cleaned = []
            for item in items:
                item.pop("_sk", None)
                cleaned.append(item)
            return cleaned if cleaned else None

        async def _from_live():
            from civicledger.congress.trades import fetch_all_congressional_trades
            return await fetch_all_congressional_trades(year=yr, limit=lim, detailed=detailed)

        data = await _try_dynamo_first(_from_dynamo, _from_live, f"congress/{yr}")

        if not data:
            return f"No congressional trades found for {yr}."

        return _format_result(
            data[:lim],
            f"{len(data)} House trades for {yr}"
            + (" (with transaction detail)" if detailed else "")
            + " — Senate unavailable (eFD blocks automation).",
        )

    except ValidationError as e:
        return f"Invalid input: {e}"
    except Exception as e:
        return _tool_error("get_congressional_trades", e)


# ---------------------------------------------------------------------------
# Tool 9: Economic Events (FRED)
# ---------------------------------------------------------------------------

@mcp.tool()
async def get_economic_calendar(
    from_date: str | None = None,
    to_date: str | None = None,
) -> str:
    """Fetch upcoming US economic data releases from the FRED calendar.

    Tracks major macro indicators: CPI, Non-Farm Payrolls, GDP, FOMC rate
    decisions, PPI, Retail Sales, ISM PMI, Consumer Confidence, Durable
    Goods, Housing Starts, and more. Each event includes impact level
    (high/medium/low) and a description.

    Requires a free FRED API key (set CIVICLEDGER_FRED_API_KEY env var).
    Get one at: https://fred.stlouisfed.org/docs/api/api_key.html

    Source: FRED API (Federal Reserve Bank of St. Louis). Public domain US
    government data. "This product uses the FRED API but is not endorsed
    or certified by the Federal Reserve Bank of St. Louis."

    Args:
        from_date: Start date in YYYY-MM-DD format. Defaults to 7 days ago.
        to_date: End date in YYYY-MM-DD format. Defaults to today.
    """
    try:
        fd, td = normalize_date_range(from_date, to_date)

        async def _from_dynamo():
            from civicledger import storage
            items = await storage.query("ECONOMIC", sk_prefix=fd[:7])
            filtered = []
            for item in items:
                item.pop("_sk", None)
                item_date = item.get("date", "")
                if fd <= item_date <= td:
                    filtered.append(item)
            return filtered if filtered else None

        async def _from_live():
            from civicledger.economic.fred import fetch_economic_events
            return await fetch_economic_events(fd, td)

        data = await _try_dynamo_first(_from_dynamo, _from_live, "economic_events")

        if not data:
            return (
                f"No major economic releases are scheduled between {fd} and {td}."
            )

        return _format_result(
            data,
            f"{len(data)} economic events from {fd} to {td}",
        )

    except FredApiKeyMissing as e:
        return str(e)
    except ValidationError as e:
        return f"Invalid input: {e}"
    except Exception as e:
        return _tool_error("get_economic_calendar", e)


# ---------------------------------------------------------------------------
# Server entry point
# ---------------------------------------------------------------------------

def run_server(transport: str = "stdio", host: str = "0.0.0.0", port: int = 8080):
    """Start the MCP server with the specified transport.

    Args:
        transport: "stdio" for local use (Claude Desktop), "sse" for remote hosting.
        host: Host to bind to (SSE only). Defaults to "0.0.0.0".
        port: Port to bind to (SSE only). Defaults to 8080.
    """
    if transport == "sse":
        # Override host/port from CLI args before starting SSE
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.run(transport="sse")
    else:
        mcp.run(transport="stdio")
