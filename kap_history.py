"""Fetch every KAP disclosure published in a date range: lists, pages, financial statements and attachments.

kap_watch.py catches new disclosures live; this script fetches the past. Run one of them at a time,
as DuckDB allows only one process to write to the database. Pages are fetched newest day first, while a
background worker reads their attachments, which KAP serves slowly. What is already complete is skipped,
so running the same command again continues where it stopped. Every disclosure whose page and
attachments have all been read is appended to data/disclosures.jsonl. Stop with Ctrl+C.

Usage:
    python kap_history.py --start 01.01.2025 --end 31.12.2025
"""
import argparse
import threading
import time
from datetime import date, datetime, timedelta

import duckdb
import httpx

import kap_attachments
import kap_details
import kap_financials
from kap_disclosures import DATE_FORMAT, HEADERS, SCHEMA, fetch_day, save_to_db
from kap_output import DATA_DIR, DB_PATH, JSONL_PATH, append_to_jsonl, export_to_excel
from kap_watch import fetch_attachment, fetch_details, log

REQUEST_DELAY = 3  # seconds between requests of each worker; KAP blocks after ~100 requests in a few minutes

LISTED_DAYS_SCHEMA = """
CREATE TABLE IF NOT EXISTS listed_days (
    day DATE PRIMARY KEY      -- past day whose complete disclosure list is in the database
)
"""


def fetch_day_pages(con: duckdb.DuckDBPyConnection, client: httpx.Client, day: date) -> None:
    """Fetch the list of `day` (once for a past day) and the pages of its disclosures that have none yet."""
    if not con.execute("SELECT count(*) FROM listed_days WHERE day = ?", [day]).fetchone()[0]:
        save_to_db(con, fetch_day(client, day))
        if day < date.today():  # today's list still grows
            con.execute("INSERT INTO listed_days VALUES (?)", [day])
        time.sleep(REQUEST_DELAY)
    todo = con.execute("""
        SELECT disclosure_index, disclosure_class FROM disclosures
        WHERE CAST(publish_date AS DATE) = ? AND disclosure_index NOT IN (SELECT disclosure_index FROM disclosure_details)
        ORDER BY publish_date DESC
    """, [day]).fetchall()
    if not todo:
        return
    log(f"{day:%d.%m.%Y}: fetching {len(todo)} pages.")
    failed = 0
    for disclosure_index, disclosure_class in todo:
        try:
            fetch_details(con, client, disclosure_index, disclosure_class)
        except Exception as e:  # retried on the next run
            failed += 1
            log(f"  page {disclosure_index} failed, will retry on the next run: {e!r}")
        time.sleep(REQUEST_DELAY)
    log(f"{day:%d.%m.%Y}: fetched {len(todo) - failed} pages, {failed} failed.")


def attachment_worker(con: duckdb.DuckDBPyConnection, start: date, end: date, pages_done: threading.Event) -> None:
    """Read unread attachments of disclosures in the range, newest first, as their pages are saved."""
    cur = con.cursor()  # DuckDB needs a separate cursor per thread
    failed = set()  # (disclosure_index, file_id); retried on the next run, not over and over
    with httpx.Client(headers=HEADERS, http2=True, timeout=120) as client:
        while True:
            last_round = pages_done.is_set()  # if so, no new attachments can appear after this query
            todo = [job for job in cur.execute("""
                SELECT a.disclosure_index, a.file_id, a.file_name, a.url
                FROM disclosure_attachments a JOIN disclosures d USING (disclosure_index)
                WHERE CAST(d.publish_date AS DATE) BETWEEN ? AND ? AND NOT EXISTS (
                    SELECT 1 FROM attachment_texts t WHERE t.disclosure_index = a.disclosure_index AND t.file_id = a.file_id)
                ORDER BY d.publish_date DESC
            """, [start, end]).fetchall() if job[:2] not in failed]
            if not todo:
                if last_round:
                    return
                time.sleep(30)  # wait for more pages
                continue
            for disclosure_index, file_id, file_name, url in todo:
                try:
                    result = fetch_attachment(cur, client, disclosure_index, file_id, url)
                    log(f"  attachment {disclosure_index} '{file_name}': {result['page_count']} pages, "
                        f"{result['ocr_pages']} OCR, {len(result['tables'])} tables")
                except Exception as e:
                    failed.add((disclosure_index, file_id))
                    log(f"  attachment {disclosure_index} '{file_name}' failed, will retry on the next run: {e!r}")
                time.sleep(REQUEST_DELAY)


def parse_day(text: str) -> date:
    return datetime.strptime(text, DATE_FORMAT).date()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch every KAP disclosure published in a date range.")
    parser.add_argument("--start", metavar="DD.MM.YYYY", type=parse_day, required=True, help="first day")
    parser.add_argument("--end", metavar="DD.MM.YYYY", type=parse_day, required=True, help="last day")
    args = parser.parse_args()
    if args.start > args.end:
        parser.error("--start is after --end")

    DATA_DIR.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA)
    con.execute(kap_details.SCHEMA)
    con.execute(kap_attachments.SCHEMA)
    con.execute(kap_financials.SCHEMA)
    con.execute(LISTED_DAYS_SCHEMA)

    pages_done = threading.Event()
    worker = threading.Thread(target=attachment_worker, args=(con, args.start, args.end, pages_done), daemon=True)
    worker.start()
    log(f"Fetching disclosures published from {args.start:%d.%m.%Y} to {args.end:%d.%m.%Y}, newest day first. "
        "Ctrl+C to stop.")
    appended = 0
    try:
        with httpx.Client(headers=HEADERS, http2=True, timeout=60) as client:
            day = args.end
            while day >= args.start:
                try:
                    fetch_day_pages(con, client, day)
                except Exception as e:  # e.g. the list request failed; the day is tried again on the next run
                    log(f"{day:%d.%m.%Y} failed, will retry on the next run: {e!r}")
                appended += append_to_jsonl(con)
                day -= timedelta(days=1)
        pages_done.set()
        log("All pages fetched; waiting for the remaining attachments.")
        while worker.is_alive():
            worker.join(5)  # short, so Ctrl+C is noticed
            appended += append_to_jsonl(con)
        log("Done.")
    except KeyboardInterrupt:
        log("Stopping. Run the same command again to continue.")
    finally:
        appended += append_to_jsonl(con)
        try:
            export_to_excel(con)
        except PermissionError:
            log("Excel file is open, so it was not updated.")
        con.close()
    log(f"Added {appended} disclosures to {JSONL_PATH}.")


if __name__ == "__main__":
    main()
