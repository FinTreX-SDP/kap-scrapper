"""Watch KAP for new disclosures of the companies in companies.txt and write each to data/disclosures.jsonl
as soon as its page and attachments are read.

Polls the disclosure list without pause and hands each new disclosure to one of WORKERS
threads, which fetches its page, financial statements and attachments, so a slow attachment (KAP sends
them slowly, and scans need OCR) never holds up other disclosures. Only disclosures published after the
watcher started are processed (minus CATCH_UP_MINUTES, so a short restart does not miss anything);
kap_history.py fetches older ones. A disclosure that fails is tried again RETRY_INTERVAL seconds later,
at most MAX_FAILURES times in all. Stop with Ctrl+C.

Usage:
    python kap_watch.py
"""
import queue
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta

import httpx

import kap_attachments
import kap_details
import kap_financials
from kap_disclosures import HEADERS, Disclosure, fetch_list, list_fields
from kap_http import ATTACHMENT_TIMEOUT, connection_options, kap_get
from kap_output import DATA_DIR, append_record, disclosure_record, stop_writing, unreadable, written_indexes

CATCH_UP_MINUTES = 10
RETRY_INTERVAL = 300  # seconds before a disclosure that failed is tried again
MAX_FAILURES = 3  # attempts per disclosure before the watcher leaves it to kap_history.py
WORKERS = 4  # new disclosures come a few at a time
# Downloading an attachment takes minutes (KAP sends ~10 KB/s per connection) but little memory; reading it
# takes seconds of CPU and most of the memory. So however many workers download, only this many read at once.
EXTRACT_SLOTS = 2
extracting = threading.BoundedSemaphore(EXTRACT_SLOTS)


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def fetch_disclosure(client: httpx.Client, file_client: httpx.Client, d: Disclosure) -> dict:
    """Fetch a disclosure's page, its financial statements if it is a financial report, and all of its
    attachments but English copies of Turkish ones, and return its record. `file_client` is for the
    attachments, which come slowly."""
    page = kap_details.parse_page(kap_details.fetch_page(client, d.disclosureIndex))
    # Other forms reuse the layout of the statements, so only financial reports (class FR) are parsed.
    items = (kap_financials.parse_financials(page["html"])
             if d.disclosureClass == "FR" and "financial-table" in page["html"] else [])
    texts = {}
    for file_id in kap_attachments.to_read({file_id: a["file_name"] for file_id, a in page["attachments"].items()}):
        a = page["attachments"][file_id]
        data = kap_get(file_client, a["url"])  # a failed download fails the disclosure, so it is tried again
        try:
            with extracting:
                texts[file_id] = result = kap_attachments.extract(data)
        except Exception as e:  # a file that cannot be read would fail every time; record it as such instead
            texts[file_id] = result = unreadable(e)
            log(f"  attachment {d.disclosureIndex} '{a['file_name']}' could not be read: {e!r}")
            continue
        log(f"  attachment {d.disclosureIndex} '{a['file_name']}': {result['page_count']} pages, "
            f"{result['ocr_pages']} OCR, {len(result['tables'])} tables")
    return disclosure_record(list_fields(d), page, items, texts)


def start_workers(count: int, jobs: queue.Queue, handle: Callable[[httpx.Client, Disclosure], None],
                  proxies: list[str] | None = None) -> None:
    """Start `count` threads that pass each disclosure put in `jobs` to `handle`, together with a client of
    their own for attachments: KAP sends ~10 KB/s per connection, so each download needs its own."""
    def work() -> None:
        with httpx.Client(headers=HEADERS, timeout=ATTACHMENT_TIMEOUT, **connection_options(proxies)) as file_client:
            while True:
                d = jobs.get()
                try:
                    handle(file_client, d)
                finally:
                    jobs.task_done()

    for _ in range(count):
        threading.Thread(target=work, daemon=True).start()


def main() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    done = written_indexes()
    running: set[int] = set()  # handed to a worker and not finished yet
    failures: Counter = Counter()  # disclosure_index -> failed attempts
    retry_at: dict[int, float] = {}  # disclosure_index -> time.monotonic() after which a failed one is tried again
    jobs: queue.Queue = queue.Queue()
    since = datetime.now() - timedelta(minutes=CATCH_UP_MINUTES)
    log(f"Watching KAP for disclosures published after {since:%d.%m %H:%M:%S}. "
        "Ctrl+C to stop.")

    with httpx.Client(headers=HEADERS, http2=True, timeout=60) as client:  # pages and lists; they are small

        def handle(file_client: httpx.Client, d: Disclosure) -> None:
            try:
                record = fetch_disclosure(client, file_client, d)
            except Exception as e:  # tried again by a later poll
                failures[d.disclosureIndex] += 1
                retry_at[d.disclosureIndex] = time.monotonic() + RETRY_INTERVAL
                log(f"{d.disclosureIndex} failed ({failures[d.disclosureIndex]}/{MAX_FAILURES}): {e!r}")
            else:
                append_record(record)
                done.add(d.disclosureIndex)
                delay = (datetime.now() - d.published).total_seconds()
                log(f"NEW {d.disclosureIndex} {d.stockCode or '-'}: {d.title} "
                    f"(published {d.published:%H:%M:%S}, written after {delay:.0f} s)")
            finally:
                running.discard(d.disclosureIndex)

        start_workers(WORKERS, jobs, handle)
        try:
            while True:
                started = time.monotonic()
                now = datetime.now()
                try:  # yesterday too, just after midnight
                    listed = fetch_list(client, (now - timedelta(minutes=1)).date(), now.date())
                except httpx.HTTPError as e:
                    log(f"Network error, trying again on the next poll: {e}")
                    listed = []
                for d in sorted(listed, key=lambda d: d.published):
                    i = d.disclosureIndex
                    if (d.published >= since and i not in done and i not in running
                            and failures[i] < MAX_FAILURES and retry_at.get(i, 0) <= started):
                        running.add(i)
                        jobs.put(d)
        except KeyboardInterrupt:
            stop_writing()
            log("Stopping." + (f" {len(running)} disclosures were not finished; kap_history.py can fetch them."
                               if running else ""))


if __name__ == "__main__":
    main()
