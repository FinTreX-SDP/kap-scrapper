"""Fetch the KAP disclosure list, store it in DuckDB and export it to Excel.

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
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

API_URL = "https://www.kap.org.tr/tr/api/disclosure/list/main"
DISCLOSURE_URL = "https://www.kap.org.tr/tr/Bildirim/{}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "tr",
    "Referer": "https://www.kap.org.tr/tr",
}
DATE_FORMAT = "%d.%m.%Y"
DATA_DIR = Path(__file__).parent / "data"
DB_PATH = DATA_DIR / "kap.duckdb"
EXCEL_PATH = DATA_DIR / "disclosures.xlsx"
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


DISCLOSURES_QUERY = "SELECT * FROM disclosures ORDER BY publish_date DESC"
# Same rows with each disclosure's detail text and attachment names, once kap_details.py has run.
DISCLOSURES_WITH_DETAILS_QUERY = """
    SELECT d.*, x.text AS detail_text,
           (SELECT string_agg(a.file_name, ' | ') FROM disclosure_attachments a
            WHERE a.disclosure_index = d.disclosure_index) AS attachment_names
    FROM disclosures d LEFT JOIN disclosure_details x USING (disclosure_index)
    ORDER BY d.publish_date DESC
"""
EXCEL_SHEETS = {  # sheet name -> (table it needs, query); sheets whose table does not exist are skipped
    "Details": ("disclosure_details", "SELECT * EXCLUDE (html) FROM disclosure_details ORDER BY disclosure_index DESC"),
    "Fields": ("disclosure_fields", "SELECT * FROM disclosure_fields ORDER BY disclosure_index DESC, row_no, value_col"),
    "Tables": ("disclosure_tables", "SELECT * FROM disclosure_tables ORDER BY disclosure_index DESC, table_no"),
}
ATTACHMENTS_QUERY = "SELECT * FROM disclosure_attachments ORDER BY disclosure_index DESC"
# Same rows with the extracted text, once kap_attachments.py has run.
ATTACHMENTS_WITH_TEXT_QUERY = """
    SELECT a.*, t.* EXCLUDE (disclosure_index, file_id)
    FROM disclosure_attachments a LEFT JOIN attachment_texts t USING (disclosure_index, file_id)
    ORDER BY a.disclosure_index DESC
"""
FINANCIALS_QUERY = """
    SELECT d.stock_code, d.company_title, f.*
    FROM financial_items f JOIN disclosures d USING (disclosure_index)
    ORDER BY f.disclosure_index DESC, f.statement, f.row_no
"""
# One Excel row per table row; the cells list is spread over separate columns.
ATTACHMENT_TABLES_QUERY = """
    SELECT t.disclosure_index, a.file_name, t.page, t.table_no, t.row_no, t.method, t.cells
    FROM attachment_tables t LEFT JOIN disclosure_attachments a USING (disclosure_index, file_id)
    ORDER BY t.disclosure_index DESC, t.file_id, t.table_no, t.row_no
"""
EXCEL_CELL_LIMIT = 32767


def excel_value(value):
    if isinstance(value, str):
        return ILLEGAL_CHARACTERS_RE.sub("", value)[:EXCEL_CELL_LIMIT]
    return value


def export_to_excel(con: duckdb.DuckDBPyConnection) -> int:
    """Write every table to its own sheet and return the number of disclosures."""
    existing = {row[0] for row in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
    has_details = {"disclosure_details", "disclosure_attachments"} <= existing
    has_texts = "attachment_texts" in existing
    sheets = {"Disclosures": ("disclosures", DISCLOSURES_WITH_DETAILS_QUERY if has_details else DISCLOSURES_QUERY),
              **EXCEL_SHEETS,
              "Attachments": ("disclosure_attachments", ATTACHMENTS_WITH_TEXT_QUERY if has_texts else ATTACHMENTS_QUERY),
              "AttachmentTables": ("attachment_tables", ATTACHMENT_TABLES_QUERY),
              "Financials": ("financial_items", FINANCIALS_QUERY)}
    wb = Workbook()
    wb.remove(wb.active)
    for sheet, (table, query) in sheets.items():
        if table not in existing:
            continue
        result = con.execute(query)
        ws = wb.create_sheet(sheet)
        ws.append([col[0] for col in result.description])
        for row in result.fetchall():
            values = []
            for v in row:
                if isinstance(v, list):  # e.g. table cells: one column each
                    values.extend(v)
                else:
                    values.append(v)
            ws.append([excel_value(v) for v in values])
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
    wb.save(EXCEL_PATH)
    return con.execute("SELECT count(*) FROM disclosures").fetchone()[0]


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
