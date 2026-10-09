"""Watch KAP for new disclosures and fetch their details and attachments as soon as they appear.

Polls the disclosure list every POLL_INTERVAL seconds. Details are fetched right away; attachments
(which may need slow OCR) are handled by a background worker so they never delay new disclosures.
Only disclosures published after the watcher started are processed (minus CATCH_UP_MINUTES, so a
short restart does not miss anything); kap_history.py fetches older ones. Stop with Ctrl+C.

Every disclosure whose page and attachments have all been read is appended to data/disclosures.jsonl.

Usage:
    python kap_watch.py
"""
import queue
import threading
import time
from collections import Counter
from datetime import datetime, timedelta

import duckdb
import httpx

import kap_attachments
import kap_details
import kap_financials
from kap_disclosures import HEADERS, REQUEST_DELAY, SCHEMA, fetch_day, save_to_db
from kap_http import RateLimited, kap_get
from kap_output import DATA_DIR, DB_PATH, append_to_jsonl, export_to_excel, write_detail_file

POLL_INTERVAL = 15  # seconds
CATCH_UP_MINUTES = 10
RETRY_INTERVAL = 300  # seconds between sweeps that re-queue failed attachments
MAX_FAILURES = 3  # detail fetch attempts per disclosure before the watcher skips it


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def fetch_details(con: duckdb.DuckDBPyConnection, client: httpx.Client, disclosure_index: int,
                  disclosure_class: str | None, title: str | None) -> dict:
    """Fetch and save a disclosure page and, for a financial report, its statements. Returns the parsed page,
    with the ids of the attachments worth reading under "to_read"; the others are marked as skipped."""
    page = kap_details.parse_page(kap_details.fetch_page(client, disclosure_index))
    page["to_read"] = kap_attachments.to_read(title, {file_id: a["file_name"] for file_id, a in page["attachments"].items()})
    # Marked before the page is saved, so no attachment worker ever sees them waiting to be read.
    kap_attachments.mark_skipped(con, disclosure_index, set(page["attachments"]) - page["to_read"])
    kap_details.save(con, disclosure_index, page)
    if disclosure_class == "FR" and "financial-table" in page["html"]:
        page["financial_items"] = kap_financials.parse_financials(page["html"])
        kap_financials.save(con, disclosure_index, page["financial_items"])
    write_detail_file(con, disclosure_index)
    return page


def fetch_attachment(con: duckdb.DuckDBPyConnection, client: httpx.Client, disclosure_index: int,
                     file_id: str, url: str) -> dict:
    """Download one attachment, extract its text and tables and save them. Returns the extraction result."""
    result = kap_attachments.extract(kap_get(client, url))
    kap_attachments.save(con, disclosure_index, file_id, result)
    write_detail_file(con, disclosure_index)
    return result


def attachment_worker(con: duckdb.DuckDBPyConnection, jobs: queue.Queue, changed: threading.Event) -> None:
    cur = con.cursor()  # DuckDB needs a separate cursor per thread
    with httpx.Client(headers=HEADERS, http2=True, timeout=120) as client:
        while True:
            disclosure_index, file_id, file_name, url = jobs.get()
            try:
                result = fetch_attachment(cur, client, disclosure_index, file_id, url)
                log(f"  attachment {disclosure_index} '{file_name}': {result['page_count']} pages, "
                    f"{result['ocr_pages']} OCR, {len(result['tables'])} tables")
                changed.set()
            except Exception as e:  # retried by the next sweep in main()
                log(f"  attachment {disclosure_index} '{file_name}' failed, will retry: {e}")
            finally:
                jobs.task_done()
            time.sleep(REQUEST_DELAY)


def queue_missing_attachments(con: duckdb.DuckDBPyConnection, jobs: queue.Queue, since: datetime) -> int:
    """Queue attachments of watched disclosures that have no extracted text yet (e.g. after a failure)."""
    missing = con.execute("""
        SELECT a.disclosure_index, a.file_id, a.file_name, a.url
        FROM disclosure_attachments a JOIN disclosures d USING (disclosure_index)
        WHERE d.publish_date >= ? AND NOT EXISTS (
            SELECT 1 FROM attachment_texts t WHERE t.disclosure_index = a.disclosure_index AND t.file_id = a.file_id)
    """, [since]).fetchall()
    for job in missing:
        jobs.put(job)
    return len(missing)


def poll(con: duckdb.DuckDBPyConnection, client: httpx.Client, since: datetime) -> list[tuple]:
    """Save the latest list and return new disclosures that still need details."""
    now = datetime.now()
    days = {now.date(), (now - timedelta(seconds=2 * POLL_INTERVAL)).date()}  # both days around midnight
    for day in sorted(days):
        save_to_db(con, fetch_day(client, day))
    return con.execute("""
        SELECT disclosure_index, publish_date, stock_code, title, disclosure_class FROM disclosures
        WHERE publish_date >= ? AND disclosure_index NOT IN (SELECT disclosure_index FROM disclosure_details)
        ORDER BY publish_date
    """, [since]).fetchall()


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA)
    con.execute(kap_details.SCHEMA)
    con.execute(kap_attachments.SCHEMA)
    con.execute(kap_financials.SCHEMA)
    kap_attachments.skip_unwanted(con)  # so the retry sweep never downloads them

    jobs: queue.Queue = queue.Queue()
    changed = threading.Event()
    threading.Thread(target=attachment_worker, args=(con, jobs, changed), daemon=True).start()

    since = datetime.now() - timedelta(minutes=CATCH_UP_MINUTES)
    log(f"Watching KAP every {POLL_INTERVAL} s for disclosures published after {since:%d.%m %H:%M:%S}. "
        "Ctrl+C to stop.")
    last_sweep = time.monotonic()
    failures: Counter = Counter()  # disclosure_index -> failed detail fetches
    try:
        with httpx.Client(headers=HEADERS, http2=True, timeout=60) as client:
            while True:
                started = time.monotonic()
                if started - last_sweep > RETRY_INTERVAL and jobs.unfinished_tasks == 0:  # worker idle
                    last_sweep = started
                    if count := queue_missing_attachments(con, jobs, since):
                        log(f"Retrying {count} attachments that failed earlier.")
                try:
                    for disclosure_index, published, stock_code, title, disclosure_class in poll(con, client, since):
                        if failures[disclosure_index] >= MAX_FAILURES:
                            continue  # left for kap_details.py, so one bad page cannot block the rest
                        try:
                            page = fetch_details(con, client, disclosure_index, disclosure_class, title)
                        except RateLimited:
                            raise
                        except Exception as e:  # retried on the next poll
                            failures[disclosure_index] += 1
                            log(f"Details of {disclosure_index} failed ({failures[disclosure_index]}/{MAX_FAILURES}): {e}")
                            continue
                        if "financial_items" in page:
                            log(f"  financial statements of {disclosure_index}: {len(page['financial_items'])} values")
                        delay = (datetime.now() - published).total_seconds()
                        log(f"NEW {disclosure_index} {stock_code or '-'}: {title} "
                            f"(published {published:%H:%M:%S}, caught after {delay:.0f} s)")
                        for file_id, a in page["attachments"].items():
                            if file_id in page["to_read"]:
                                jobs.put((disclosure_index, file_id, a["file_name"], a["url"]))
                        changed.set()
                        time.sleep(REQUEST_DELAY)
                except RateLimited:
                    log("KAP keeps rate limiting; trying again on the next poll.")
                except httpx.HTTPError as e:  # the list request itself failed
                    log(f"Network error, trying again on the next poll: {e}")

                if changed.is_set():
                    changed.clear()
                    try:
                        export_to_excel(con)
                    except PermissionError:
                        log("Excel file is open, so it was not updated. Close it to get updates.")
                append_to_jsonl(con)
                time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - started)))
    except KeyboardInterrupt:
        log("Stopping.")
    finally:
        pending = jobs.qsize()
        if pending:
            log(f"{pending} attachments were still queued; run kap_attachments.py to process them.")
        append_to_jsonl(con)
        try:
            export_to_excel(con)
        except PermissionError:
            log("Excel file is open, so it was not updated.")
        con.close()


if __name__ == "__main__":
    main()
