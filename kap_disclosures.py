"""Fetch the KAP disclosure list, store it in DuckDB and update the Excel summary.

Only disclosures that concern a company in bist100.txt are kept: as soon as a list arrives, the
others are dropped, so they never reach the database and their pages are never requested.

Usage:
    python kap_disclosures.py                                  # today
    python kap_disclosures.py --start 01.10.2026 --end 05.10.2026
"""
import argparse
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb
import httpx
import polars as pl
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from kap_http import throttle
from kap_output import DATA_DIR, DB_PATH, EXCEL_PATH, export_to_excel

API_URL = "https://www.kap.org.tr/tr/api/disclosure/list/main"
DISCLOSURE_URL = "https://www.kap.org.tr/tr/Bildirim/{}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "tr",
    "Referer": "https://www.kap.org.tr/tr",
}
DATE_FORMAT = "%d.%m.%Y"
REQUEST_DELAY = 1.0  # seconds between requests, to keep the load on KAP low
WATCHLIST_PATH = Path(__file__).parent / "bist100.txt"


def load_watchlist() -> set[str]:
    """Stock codes in bist100.txt: the first word of each line that is neither empty nor a # comment."""
    lines = WATCHLIST_PATH.read_text(encoding="utf-8").splitlines()
    return {line.split()[0] for line in lines if line.strip() and not line.startswith("#")}


WATCHLIST = load_watchlist()

SCHEMA = """
CREATE TABLE IF NOT EXISTS disclosures (
    disclosure_index INTEGER PRIMARY KEY,
    disclosure_id VARCHAR,
    publish_date TIMESTAMP,
    stock_code VARCHAR,
    company_title VARCHAR,
    title VARCHAR,
    summary VARCHAR,
    disclosure_class VARCHAR,
    disclosure_type VARCHAR,
    disclosure_category VARCHAR,
    period VARCHAR,
    year INTEGER,
    attachment_count INTEGER,
    is_late BOOLEAN,
    related_stocks VARCHAR,
    url VARCHAR,
    mkk_member_oid VARCHAR,             -- KAP's id of the publishing member; unlike its stock code it never changes
    related_disclosure_oid VARCHAR,     -- disclosure_id of the earlier disclosure this one continues, updates or corrects
    is_changed VARCHAR,                 -- 'DUZENLENEN' (updated) or 'DUZELTILEN' (corrected); NULL for an original
    has_multi_language_support VARCHAR, -- 'Y' when the disclosure also has an English version
    term INTEGER                        -- KAP's 'donem', e.g. the quarter of a financial report
)
"""


class Disclosure(BaseModel):
    disclosureIndex: int
    disclosureId: str
    publishDate: str
    stockCode: str | None
    companyTitle: str | None
    title: str | None
    summary: str | None
    disclosureClass: str | None
    disclosureType: str | None
    disclosureCategory: str | None
    period: str | None
    year: int | None
    attachmentCount: int | None
    isLate: bool | None
    relatedStocks: str | None
    mkkMemberOid: str | None
    relatedDisclosureOid: str | None
    isChanged: str | None
    hasMultiLanguageSupport: str | None
    donem: int | None


def stock_codes(text: str | None) -> set[str]:
    return {code.strip() for code in (text or "").split(",") if code.strip()}  # e.g. "ISATR, ISBTR, ISCTR"


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30), reraise=True)
def fetch_list(client: httpx.Client, start: date, end: date) -> list[Disclosure]:
    """Return the disclosures published from `start` to `end` (both included) that concern a company in
    the watchlist, as its own disclosure or as one of its related stocks."""
    throttle.wait()
    r = client.post(API_URL, json={"fromDate": start.strftime(DATE_FORMAT), "toDate": end.strftime(DATE_FORMAT),
                                   "memberTypes": ["IGS", "DDK"]})
    r.raise_for_status()
    disclosures = [Disclosure(**item["disclosureBasic"]) for item in r.json()]
    return [d for d in disclosures if (stock_codes(d.stockCode) | stock_codes(d.relatedStocks)) & WATCHLIST]


def months(start: date, end: date) -> list[tuple[date, date]]:
    """Split `start`..`end` into calendar-month ranges, newest first. KAP lists a whole month in one response."""
    ranges = []
    while end >= start:
        first = max(start, end.replace(day=1))
        ranges.append((first, end))
        end = first - timedelta(days=1)
    return ranges


def save_to_db(con: duckdb.DuckDBPyConnection, disclosures: list[Disclosure]) -> None:
    if not disclosures:
        return
    df = pl.DataFrame([(
        d.disclosureIndex, d.disclosureId,
        datetime.strptime(d.publishDate, "%d.%m.%Y %H:%M:%S"),
        d.stockCode, d.companyTitle, d.title, d.summary,
        d.disclosureClass, d.disclosureType, d.disclosureCategory,
        d.period, d.year, d.attachmentCount, d.isLate, d.relatedStocks,
        DISCLOSURE_URL.format(d.disclosureIndex),
        d.mkkMemberOid, d.relatedDisclosureOid, d.isChanged, d.hasMultiLanguageSupport, d.donem,
    ) for d in disclosures], orient="row", infer_schema_length=None)  # all rows: early ones may lack e.g. a stock code
    con.execute("INSERT OR REPLACE INTO disclosures SELECT * FROM df")


def main() -> None:
    today = date.today().strftime(DATE_FORMAT)
    parser = argparse.ArgumentParser(description="Fetch the KAP disclosure list.")
    parser.add_argument("--start", default=today, help="DD.MM.YYYY (default: today)")
    parser.add_argument("--end", default=today, help="DD.MM.YYYY (default: today)")
    args = parser.parse_args()
    start = datetime.strptime(args.start, DATE_FORMAT).date()
    end = datetime.strptime(args.end, DATE_FORMAT).date()

    DATA_DIR.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA)

    fetched = 0
    with httpx.Client(headers=HEADERS, http2=True, timeout=60) as client:
        for first, last in tqdm(months(start, end), desc="Months"):
            disclosures = fetch_list(client, first, last)
            save_to_db(con, disclosures)
            fetched += len(disclosures)
            time.sleep(REQUEST_DELAY)

    total = export_to_excel(con)
    con.close()
    print(f"Fetched this run: {fetched} disclosures")
    print(f"Total in database: {total} disclosures")
    print(f"Database: {DB_PATH}")
    print(f"Excel:    {EXCEL_PATH}")


if __name__ == "__main__":
    main()
