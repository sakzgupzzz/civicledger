# CivicLedger

US financial intelligence from public domain sources.

All data comes from US government APIs — SEC EDGAR, FRED, Senate/House disclosures. No private data providers, no scraping commercial websites, no API keys required (except FRED, which is free).

## What's Included

| Module | Source | Data |
|--------|--------|------|
| **Fundamentals** | SEC EDGAR XBRL | Revenue, margins, ratios, growth for ~5,000 public companies |
| **Earnings Calendar** | SEC EDGAR EFTS | Earnings announcement dates from 8-K Item 2.02 filings |
| **Insider Trades** | SEC EDGAR Form 4 | Officer/director buys and sells — with shares, price, and value parsed from each filing |
| **Institutional Holdings** | SEC EDGAR 13F | What hedge funds and institutions own (ticker, shares, USD value per position) |
| **Congressional Trades** | House Clerk | Stock trades by members of the US House — ticker, type, amount, and dates parsed from PTR PDFs |
| **Economic Calendar** | FRED API | CPI, NFP, FOMC, GDP release dates |
| **Material Events** | SEC EDGAR 8-K | Mergers, leadership changes, restructuring |

## Install

```bash
pip install civicledger
```

For the API server:
```bash
pip install civicledger[server]
```

## Quick Start

### As a library

```python
import asyncio
from civicledger.edgar.fundamentals import fetch_fundamentals
from civicledger.edgar.earnings import fetch_earnings

async def main():
    # Get fundamentals for all US public companies
    fundamentals = await fetch_fundamentals()
    aapl = fundamentals.get("AAPL")
    print(f"AAPL revenue: ${aapl['revenue']:,.0f}")
    print(f"AAPL profit margin: {aapl['profit_margin']:.1%}")

    # Get this week's earnings
    earnings = await fetch_earnings("2026-03-17", "2026-03-21")
    for e in earnings[:5]:
        print(f"{e['filing_date']} - {e['ticker']} - {e['company']}")

asyncio.run(main())
```

### CLI

```bash
# Fetch fundamentals for all companies
civicledger refresh fundamentals

# Fetch earnings for a date range
civicledger refresh earnings --from 2026-03-01 --to 2026-03-31

# Fetch insider trades
civicledger refresh insider-trades --from 2026-03-01 --to 2026-03-07

# Fetch congressional trades
civicledger refresh congress --year 2026

# Fetch everything
civicledger refresh all
```

### API Server

```bash
civicledger serve --port 8080
```

Then open <http://localhost:8080/> for the **web dashboard** — browse insider
trades, 13F holdings, congressional trades, earnings, 8-K events, and the
economic calendar, with ticker search. Interactive API docs are at `/docs`.

Responses are cached locally on disk (`~/.cache/civicledger`, configurable via
`CIVICLEDGER_CACHE_DIR`) so repeated loads are instant. Clear it with
`civicledger cache clear`, or disable with `CIVICLEDGER_CACHE_ENABLED=false`.

Endpoints:
- `GET /ticker/{symbol}` — **Unified profile**: fundamentals + insider + 13F holders + congress + filings in one call
- `GET /fundamentals` — All company fundamentals
- `GET /fundamentals/{ticker}` — Single company (fast `companyfacts` path)
- `GET /earnings?from_date=...&to_date=...` — Earnings calendar
- `GET /insider-trades?ticker=AAPL` — Insider transactions
- `GET /insider-trades/{ticker}` — Detailed insider trades
- `GET /institutions` — Top institutional investors
- `GET /institutions/{manager}` — 13F holdings for a manager
- `GET /institutions/{manager}/changes` — Quarter-over-quarter buys/sells (new/exited/increased/decreased)
- `GET /trending/congress` · `GET /trending/insider` — Leaderboards: most-traded tickers
- `GET /search?q=...` — Full-text search across all EDGAR filings
- `GET /filings/{ticker}` — Recent filings for a company (any form), with links
- `GET /congress?detailed=true` — House stock trades (`detailed=true` parses PTR PDFs for ticker/amount/type)
- `GET /congress/house?detailed=true` — House trades only
- `GET /economic-events` — FRED economic calendar (requires a free FRED API key)
- `GET /material-events?item=5.02` — 8-K material events

> **Congressional data is House-only.** The Senate eFD site
> (efdsearch.senate.gov) blocks automated access, so `/congress/senate`
> returns `501 Not Implemented`. Use `/congress` or `/congress/house`.

## Data Sources

All public domain:

- **SEC EDGAR** — [sec.gov](https://www.sec.gov/search-filings/edgar-application-programming-interfaces) — No API key, 10 req/sec
- **FRED** — [fred.stlouisfed.org](https://fred.stlouisfed.org/) — Free API key, attribution required
- **House Clerk** — [disclosures-clerk.house.gov](https://disclosures-clerk.house.gov/) — Public congressional disclosures (House only; Senate eFD blocks automated access)

### Attribution

This product uses the FRED API but is not endorsed or certified by the Federal Reserve Bank of St. Louis.

## License

MIT
