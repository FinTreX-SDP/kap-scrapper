"""Fetch the KAP disclosure list, store it in DuckDB and update the Excel summary.

Usage:
    python kap_disclosures.py                                  # today
    python kap_disclosures.py --start 01.10.2026 --end 05.10.2026
"""
import argparse
import time
from datetime import date, datetime, timedelta

import duckdb
import httpx
import polars as pl
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

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
    url VARCHAR
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


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30), reraise=True)
def fetch_day(client: httpx.Client, day: date) -> list[Disclosure]:
    d = day.strftime(DATE_FORMAT)
    r = client.post(API_URL, json={"fromDate": d, "toDate": d, "memberTypes": ["IGS", "DDK"]})
    r.raise_for_status()
    return [Disclosure(**item["disclosureBasic"]) for item in r.json()]


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
    ) for d in disclosures], orient="row")
    con.execute("INSERT OR REPLACE INTO disclosures SELECT * FROM df")


def main() -> None:
    today = date.today().strftime(DATE_FORMAT)
    parser = argparse.ArgumentParser(description="Fetch the KAP disclosure list.")
    parser.add_argument("--start", default=today, help="DD.MM.YYYY (default: today)")
    parser.add_argument("--end", default=today, help="DD.MM.YYYY (default: today)")
    args = parser.parse_args()
    start = datetime.strptime(args.start, DATE_FORMAT).date()
    end = datetime.strptime(args.end, DATE_FORMAT).date()
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]

    DATA_DIR.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA)

    fetched = 0
    with httpx.Client(headers=HEADERS, http2=True, timeout=60) as client:
        for day in tqdm(days, desc="Days"):
            disclosures = fetch_day(client, day)
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
