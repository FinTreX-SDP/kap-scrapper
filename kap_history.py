"""Fetch the KAP disclosures of the companies in bist100.txt published in a date range: lists, pages,
financial statements and attachments.

kap_watch.py catches new disclosures live; this script fetches the past. Run one of them at a time,
as DuckDB allows only one process to write to the database. Lists are fetched a month at a time, as KAP
returns a whole month in one response. Pages are fetched newest day first, several at a time, while
background workers read their attachments, which KAP serves slowly. What is already complete is skipped, so running the
same command again continues where it stopped. Every disclosure whose page and attachments have all
been read is appended to data/disclosures.jsonl. Stop with Ctrl+C.

Usage:
    python kap_history.py --start 01.01.2025 --end 31.12.2025
"""
import argparse
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, timedelta

import duckdb
import httpx

import kap_attachments
import kap_details
import kap_financials
from kap_disclosures import DATE_FORMAT, HEADERS, SCHEMA, fetch_list, months, save_to_db
from kap_http import ATTACHMENT_TIMEOUT
from kap_output import DATA_DIR, DB_PATH, JSONL_PATH, append_to_jsonl, export_to_excel
from kap_watch import fetch_attachment, fetch_details, log

# Requests are spaced out across all threads by kap_http's throttle, so the workers only overlap KAP's
# slow answers: a page takes 0.4-9 s to come, an attachment several minutes at ~10 KB/s per connection.
# KAP kept that speed on each of 9 connections at once; 8 attachment workers stay under ~105 KB/s in total.
PAGE_WORKERS = 4
ATTACHMENT_WORKERS = 8


def fetch_page(con: duckdb.DuckDBPyConnection, client: httpx.Client, disclosure_index: int,
               disclosure_class: str | None, title: str | None) -> bool:
    """Fetch one page in a worker thread. Returns False if it failed; it is retried on the next run."""
    cur = con.cursor()  # DuckDB needs a separate cursor per thread
    try:
        fetch_details(cur, client, disclosure_index, disclosure_class, title)
        return True
    except Exception as e:
        log(f"  page {disclosure_index} failed, will retry on the next run: {e!r}")
        return False
    finally:
        cur.close()


def fetch_day_pages(con: duckdb.DuckDBPyConnection, client: httpx.Client, day: date) -> None:
    """Fetch the pages of the disclosures listed for `day` that have none yet, PAGE_WORKERS at a time."""
    todo = con.execute("""
        SELECT disclosure_index, disclosure_class, title FROM disclosures
        WHERE CAST(publish_date AS DATE) = ? AND disclosure_index NOT IN (SELECT disclosure_index FROM disclosure_details)
        ORDER BY publish_date DESC
    """, [day]).fetchall()
    if not todo:
        return
    log(f"{day:%d.%m.%Y}: fetching {len(todo)} pages.")
    pool = ThreadPoolExecutor(PAGE_WORKERS)
    futures = [pool.submit(fetch_page, con, client, *job) for job in todo]
    try:
        while not all(f.done() for f in futures):
            wait(futures, timeout=5)  # short, so Ctrl+C is noticed
    finally:
        pool.shutdown(cancel_futures=True)  # after Ctrl+C, start no more pages
    failed = sum(not f.result() for f in futures)
    log(f"{day:%d.%m.%Y}: fetched {len(todo) - failed} pages, {failed} failed.")


def read_attachments(con: duckdb.DuckDBPyConnection, jobs: queue.Queue) -> None:
    """Worker thread: download and read the attachments put in `jobs`."""
    cur = con.cursor()  # DuckDB needs a separate cursor per thread
    with httpx.Client(headers=HEADERS, http2=True, timeout=ATTACHMENT_TIMEOUT) as client:
        while True:
            disclosure_index, file_id, file_name, url = jobs.get()
            try:
                result = fetch_attachment(cur, client, disclosure_index, file_id, url)
                log(f"  attachment {disclosure_index} '{file_name}': {result['page_count']} pages, "
                    f"{result['ocr_pages']} OCR, {len(result['tables'])} tables")
            except Exception as e:
                log(f"  attachment {disclosure_index} '{file_name}' failed, will retry on the next run: {e!r}")
            finally:
                jobs.task_done()


def attachment_worker(con: duckdb.DuckDBPyConnection, start: date, end: date, pages_done: threading.Event) -> None:
    """Queue unread attachments of disclosures in the range, newest first, as their pages are saved, for
    ATTACHMENT_WORKERS threads to read. Returns once all pages are fetched and every queued attachment is done."""
    cur = con.cursor()  # DuckDB needs a separate cursor per thread
    jobs: queue.Queue = queue.Queue()
    for _ in range(ATTACHMENT_WORKERS):
        threading.Thread(target=read_attachments, args=(con, jobs), daemon=True).start()
    queued = set()  # (disclosure_index, file_id) queued in this run; one that fails is retried on the next run
    while True:
        last_round = pages_done.is_set()  # if so, no new attachments can appear after this query
        for job in cur.execute("""
            SELECT a.disclosure_index, a.file_id, a.file_name, a.url
            FROM disclosure_attachments a JOIN disclosures d USING (disclosure_index)
            WHERE CAST(d.publish_date AS DATE) BETWEEN ? AND ? AND NOT EXISTS (
                SELECT 1 FROM attachment_texts t WHERE t.disclosure_index = a.disclosure_index AND t.file_id = a.file_id)
            ORDER BY d.publish_date DESC
        """, [start, end]).fetchall():
            if job[:2] not in queued:
                queued.add(job[:2])
                jobs.put(job)
        if last_round:
            jobs.join()
            return
        time.sleep(30)  # wait for more pages


def parse_day(text: str) -> date:
    return datetime.strptime(text, DATE_FORMAT).date()


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch the KAP disclosures of the watched companies in a date range.")
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
    kap_attachments.skip_unwanted(con)

    pages_done = threading.Event()
    worker = threading.Thread(target=attachment_worker, args=(con, args.start, args.end, pages_done), daemon=True)
    worker.start()
    log(f"Fetching disclosures published from {args.start:%d.%m.%Y} to {args.end:%d.%m.%Y}, newest day first. "
        "Ctrl+C to stop.")
    appended = 0
    try:
        with httpx.Client(headers=HEADERS, http2=True, timeout=60) as client:
            for first, last in months(args.start, args.end):
                try:
                    disclosures = fetch_list(client, first, last)
                except Exception as e:  # the month is tried again on the next run
                    log(f"List of {first:%d.%m.%Y}-{last:%d.%m.%Y} failed, will retry on the next run: {e!r}")
                    continue
                save_to_db(con, disclosures)
                log(f"{first:%d.%m.%Y}-{last:%d.%m.%Y}: {len(disclosures)} disclosures of the watched companies.")
                day = last
                while day >= first:
                    fetch_day_pages(con, client, day)
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
