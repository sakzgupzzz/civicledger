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
PTR_PDF_URL = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc_id}.pdf"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; CivicLedger/0.1; +https://github.com/sakzgupzzz/civicledger)",
}

# --- PTR PDF parsing --------------------------------------------------------
# House PTR PDFs are e-filed text PDFs. Each transaction reflows across two
# lines; we anchor on the transaction code + two dates + amount range, then
# scan the surrounding context for the ticker and asset-type code. Best effort:
# older scanned/handwritten filings yield no text and are skipped.
_PTR_ANCHOR = re.compile(
    r"\b([PSE])\s+(?:\(partial\)\s+)?"
    r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})"
    r"\s+\$([\d,]+)\s*-\s*(.*?)\$([\d,]+)(?!\d*\?)",
    re.S,
)
_PTR_TICKER = re.compile(r"\(([A-Z]{1,5})\)")
_PTR_TYPECODE = re.compile(r"\[([A-Z]{2,4})\]")
# Filing-status / sub-transaction markers, e.g. "F S : New", "S O : LIVTR".
_PTR_BOUNDARY = re.compile(r"[FS]\s+[SO]\s*:\s*\S+|:\s*New|:\s*Amended|:\s*Partially|\$200\?")
_PTR_STATUS = re.compile(r"[FS]\s+[SO]\s*:\s*\S+")
_PTR_HEADER = re.compile(r"ID\s+Owner\s+Asset.*?Gains\s*>", re.S)
_PTR_OWNER = re.compile(r"^(JT|SP|DC|C)\s+")
_PTR_TXN_MAP = {"P": "purchase", "S": "sale", "E": "exchange"}


def _ptr_date(s: str) -> str:
    m, d, y = s.split("/")
    return f"{y}-{m}-{d}"


def _ptr_clean_asset(s: str) -> Optional[str]:
    s = _PTR_HEADER.sub(" ", s)
    s = _PTR_STATUS.sub(" ", s)
    s = re.sub(r"\[[A-Z]{2,4}\]", " ", s)
    s = re.sub(r"\([A-Z]{1,5}\)", " ", s)
    s = re.sub(r"\(partial\)", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .,->?")
    s = _PTR_OWNER.sub("", s).strip(" .,->?")
    return (s[:80].rstrip() if len(s) > 80 else s) or None


def parse_ptr_text(text: str) -> List[Dict[str, Any]]:
    """Extract individual stock/asset transactions from PTR PDF text.

    Returns list of {ticker, asset_description, asset_type, transaction_type,
    transaction_date, notification_date, amount_range}.
    """
    trades: List[Dict[str, Any]] = []
    seen: set = set()
    for m in _PTR_ANCHOR.finditer(text):
        code, tdate, ndate, low, mid, high = m.groups()
        pre = text[max(0, m.start() - 180): m.start()]
        post = text[m.end(): m.end() + 90]
        tk = _PTR_TICKER.search(mid) or _PTR_TICKER.search(post) or _PTR_TICKER.search(pre)
        tc = _PTR_TYPECODE.search(mid) or _PTR_TYPECODE.search(post) or _PTR_TYPECODE.search(pre)
        bnds = list(_PTR_BOUNDARY.finditer(pre))
        pre_tail = pre[bnds[-1].end():] if bnds else pre
        asset = _ptr_clean_asset(pre_tail + " " + mid)
        rec = {
            "ticker": tk.group(1) if tk else None,
            "asset_description": asset,
            "asset_type": tc.group(1) if tc else None,
            "transaction_type": _PTR_TXN_MAP.get(code),
            "transaction_date": _ptr_date(tdate),
            "notification_date": _ptr_date(ndate),
            "amount_range": f"${low} - ${high}",
        }
        key = (rec["ticker"], rec["transaction_date"], rec["amount_range"], asset)
        if key not in seen:
            seen.add(key)
            trades.append(rec)
    return trades


async def parse_ptr_pdf(
    year: int, doc_id: str, client: Optional[httpx.AsyncClient] = None
) -> List[Dict[str, Any]]:
    """Download and parse one PTR PDF into its individual transactions."""
    try:
        import pdfplumber  # noqa: F401
    except ImportError:
        logger.warning("pdfplumber not installed — PTR transaction detail unavailable")
        return []

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=60, follow_redirects=True)
    try:
        resp = await client.get(PTR_PDF_URL.format(year=year, doc_id=doc_id), headers=_HEADERS)
        if resp.status_code != 200 or resp.content[:4] != b"%PDF":
            return []
        content = resp.content
    except Exception as e:  # noqa: BLE001
        logger.debug(f"PTR PDF download failed ({doc_id}): {e}")
        return []
    finally:
        if owns_client:
            await client.aclose()

    def _extract() -> str:
        import pdfplumber
        try:
            with pdfplumber.open(io.BytesIO(content)) as pdf:
                return "\n".join((p.extract_text() or "") for p in pdf.pages)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"PTR PDF text extract failed ({doc_id}): {e}")
            return ""

    text = await asyncio.to_thread(_extract)
    return parse_ptr_text(text) if text else []


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

    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url, headers=_HEADERS)
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


async def fetch_house_trades_detailed(
    year: Optional[int] = None,
    limit: int = 200,
    max_pdf_parse: int = 40,
) -> List[Dict[str, Any]]:
    """Fetch House trades with actual transaction detail parsed from PTR PDFs.

    Downloads the disclosure index, then parses up to ``max_pdf_parse`` of the
    most recent PTR PDFs to extract real ticker, transaction type, transaction
    date, and dollar amount range. Returns one row per individual transaction.

    Args:
        year: Year to fetch. Defaults to current year.
        limit: Max transaction rows to return.
        max_pdf_parse: Cap on PDFs downloaded/parsed (each is a network call).

    Returns list of {politician, chamber, state, ticker, asset_description,
    asset_type, transaction_type, transaction_date, notification_date,
    amount_range, disclosure_date, doc_id, source_url}.
    """
    if year is None:
        year = date.today().year

    disclosures = await fetch_house_trades(year=year, limit=max_pdf_parse)
    if not disclosures:
        return []

    rows: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(5)

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        async def _enrich(disc: Dict[str, Any]) -> List[Dict[str, Any]]:
            doc_id = disc.get("doc_id")
            if not doc_id:
                return []
            async with sem:
                txns = await parse_ptr_pdf(year, doc_id, client=client)
            out = []
            for t in txns:
                out.append({
                    "politician": disc.get("politician"),
                    "chamber": "house",
                    "state": disc.get("state"),
                    "disclosure_date": disc.get("disclosure_date"),
                    "doc_id": doc_id,
                    "source_url": disc.get("source_url"),
                    **t,
                })
            return out

        results = await asyncio.gather(*[_enrich(d) for d in disclosures])

    for batch in results:
        rows.extend(batch)
    rows.sort(key=lambda t: t.get("transaction_date") or "", reverse=True)
    logger.info(
        f"House detailed trades: {len(rows)} transactions from "
        f"{min(len(disclosures), max_pdf_parse)} PTR filings for {year}"
    )
    return rows[:limit]


async def fetch_all_congressional_trades(
    year: Optional[int] = None,
    limit: int = 500,
    detailed: bool = False,
    max_pdf_parse: int = 40,
) -> List[Dict[str, Any]]:
    """Fetch congressional trades from the House clerk (Senate not automatable).

    The Senate eFD site blocks server IPs, so only House data is available via
    bulk download. Set ``detailed=True`` to parse PTR PDFs for real ticker,
    amount, and transaction-type detail (slower; bounded by ``max_pdf_parse``).
    """
    if year is None:
        year = date.today().year

    if detailed:
        trades = await fetch_house_trades_detailed(
            year=year, limit=limit, max_pdf_parse=max_pdf_parse
        )
    else:
        trades = await fetch_house_trades(year=year, limit=limit)

    logger.info(
        f"Congressional trades total: {len(trades)} "
        f"(House only — Senate eFD blocks automated access)"
    )
    return trades
