"""Input validation and normalization for CivicLedger tools and API.

Keeps validation logic in one place so the MCP server, FastAPI server, and
CLI all reject bad input the same way — with clear, user-facing messages
instead of raw tracebacks or silent empty results.
"""

import re
from datetime import date, datetime, timedelta

# A ticker is 1-5 letters, optionally a class/share suffix like "BRK.A" or "BRK-B".
_TICKER_RE = re.compile(r"^[A-Z]{1,5}([.\-][A-Z]{1,4})?$")
_DATE_FMT = "%Y-%m-%d"

# 8-K item codes the SEC currently defines (used to validate item_filter).
VALID_8K_ITEMS = {
    "1.01", "1.02", "1.03", "1.04", "1.05",
    "2.01", "2.02", "2.03", "2.04", "2.05", "2.06",
    "3.01", "3.02", "3.03",
    "4.01", "4.02",
    "5.01", "5.02", "5.03", "5.04", "5.05", "5.06", "5.07", "5.08",
    "6.01", "6.02", "6.03", "6.04", "6.05",
    "7.01",
    "8.01",
    "9.01",
}


class ValidationError(ValueError):
    """Raised when user input fails validation. Message is safe to show users."""


def normalize_ticker(ticker: str) -> str:
    """Uppercase and validate a stock ticker.

    Raises ValidationError if the ticker is not a plausible symbol. This also
    guards against injection into downstream queries.
    """
    if not ticker or not ticker.strip():
        raise ValidationError("Ticker cannot be empty.")
    t = ticker.strip().upper()
    if not _TICKER_RE.match(t):
        raise ValidationError(
            f"'{ticker}' is not a valid ticker. Expected 1-5 letters, "
            "optionally with a class suffix (e.g., AAPL, BRK.A, BRK-B)."
        )
    return t


def normalize_date(value: str, field: str = "date") -> str:
    """Validate a YYYY-MM-DD date string and return it normalized.

    Accepts a real calendar date only; rejects malformed strings and impossible
    dates (e.g., 2026-13-40).
    """
    if not value or not value.strip():
        raise ValidationError(f"{field} cannot be empty.")
    v = value.strip()
    try:
        parsed = datetime.strptime(v, _DATE_FMT).date()
    except ValueError:
        raise ValidationError(
            f"{field} '{value}' is not a valid date. Use YYYY-MM-DD (e.g., 2026-03-17)."
        )
    return parsed.isoformat()


def normalize_date_range(
    from_date: str | None,
    to_date: str | None,
    *,
    default_lookback_days: int = 7,
    today: date | None = None,
    max_span_days: int | None = None,
) -> tuple[str, str]:
    """Validate and default a (from_date, to_date) pair.

    - Missing from_date defaults to ``default_lookback_days`` ago.
    - Missing to_date defaults to today.
    - Ensures from_date <= to_date.
    - Optionally caps the span at ``max_span_days`` to avoid runaway queries.
    """
    today = today or date.today()
    fd = normalize_date(from_date, "from_date") if from_date else (
        today - timedelta(days=default_lookback_days)
    ).isoformat()
    td = normalize_date(to_date, "to_date") if to_date else today.isoformat()

    if fd > td:
        raise ValidationError(
            f"from_date ({fd}) must be on or before to_date ({td})."
        )

    if max_span_days is not None:
        span = (date.fromisoformat(td) - date.fromisoformat(fd)).days
        if span > max_span_days:
            raise ValidationError(
                f"Date range of {span} days exceeds the maximum of "
                f"{max_span_days} days. Narrow the range."
            )
    return fd, td


def normalize_limit(limit: int, *, default: int = 100, maximum: int = 1000) -> int:
    """Clamp a limit into [1, maximum], falling back to ``default`` on bad input."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n < 1:
        return default
    return min(n, maximum)


def normalize_8k_item(item: str | None) -> str | None:
    """Validate an 8-K item code (e.g., '5.02'). None passes through unchanged."""
    if item is None or not str(item).strip():
        return None
    code = str(item).strip()
    if code not in VALID_8K_ITEMS:
        raise ValidationError(
            f"'{item}' is not a recognized 8-K item code. Examples: "
            "1.01 (material agreement), 2.01 (acquisition), 2.02 (earnings), "
            "5.02 (officer change), 7.01 (Reg FD), 8.01 (other)."
        )
    return code


def normalize_year(year: int | None, *, today: date | None = None) -> int:
    """Validate a 4-digit year within a sane window (EDGAR starts ~1994)."""
    today = today or date.today()
    if year is None:
        return today.year
    try:
        y = int(year)
    except (TypeError, ValueError):
        raise ValidationError(f"'{year}' is not a valid year.")
    if y < 1994 or y > today.year + 1:
        raise ValidationError(
            f"Year {y} is out of range (expected 1994-{today.year + 1})."
        )
    return y


def iter_months(from_date: str, to_date: str):
    """Yield (year, month) tuples covering every month in the inclusive range.

    Used by callers that must issue one query per month (e.g., the FRED
    releases-dates endpoint) so multi-month ranges are not silently truncated.
    """
    start = date.fromisoformat(from_date).replace(day=1)
    end = date.fromisoformat(to_date).replace(day=1)
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
