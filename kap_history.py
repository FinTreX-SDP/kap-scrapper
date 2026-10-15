"""Fetch the KAP disclosures of the companies in companies.txt published in a date range and write each to
data/disclosures.jsonl once its page, financial statements and attachments are read.

kap_watch.py catches new disclosures live; this script fetches the past. Run one of them at a time, as
both write the same file. Lists are fetched a month at a time, as KAP returns a whole month in one
response, and their disclosures are fetched newest first by WORKERS threads. Disclosures already in the
file are skipped, so running the same command again continues where it stopped; a disclosure that
failed is tried again then. Stop with Ctrl+C.

Usage:
    python kap_history.py --start 01.01.2025 --end 31.12.2025
"""
import argparse
import queue
import time
from collections.abc import Callable
from datetime import date, datetime

import httpx

from kap_disclosures import DATE_FORMAT, HEADERS, Disclosure, fetch_list, months
from kap_output import DATA_DIR, JSONL_PATH, append_record, stop_writing, written_indexes
from kap_watch import fetch_disclosure, log, start_workers

# Requests are spaced out across all threads by kap_http's throttle, so the workers only overlap KAP's
# slow answers: a page takes 0.4-9 s to come, an attachment several minutes at ~10 KB/s per connection.
# KAP kept 10-30 KB/s on each of 12 connections at once without a stall (~230 KB/s in total), which is
# about all the line takes. While some workers wait for attachments, the others go on with pages.
WORKERS = 12
PROGRESS_INTERVAL = 60  # seconds between progress lines


def fetch_range(start: date, end: date, skip: set[int], write: Callable[[dict], None]) -> None:
    """Fetch the disclosures published from `start` to `end` whose disclosure_index is not in `skip`, and pass
    each record to `write`, from the worker threads, once it is complete. A disclosure that fails is logged
    and left out. Returns when every disclosure has been tried."""
    jobs: queue.Queue = queue.Queue()
    with httpx.Client(headers=HEADERS, http2=True, timeout=60) as client:  # pages and lists; they are small

        def handle(file_client: httpx.Client, d: Disclosure) -> None:
            try:
                write(fetch_disclosure(client, file_client, d))
            except Exception as e:
                log(f"  {d.disclosureIndex} failed, will retry on the next run: {e!r}")

        start_workers(WORKERS, jobs, handle)
        for first, last in months(start, end):
            try:
                listed = fetch_list(client, first, last)
            except Exception as e:  # the month is tried again on the next run
                log(f"List of {first:%d.%m.%Y}-{last:%d.%m.%Y} failed, will retry on the next run: {e!r}")
                continue
            todo = sorted((d for d in listed if d.disclosureIndex not in skip), key=lambda d: d.published, reverse=True)
            log(f"{first:%d.%m.%Y}-{last:%d.%m.%Y}: {len(listed)} disclosures of the watched companies, "
                f"{len(todo)} to fetch.")
            for d in todo:
                jobs.put(d)
        last_progress = time.monotonic()
        while jobs.unfinished_tasks:
            time.sleep(1)  # short, so Ctrl+C is noticed
            if time.monotonic() - last_progress > PROGRESS_INTERVAL:
                last_progress = time.monotonic()
                log(f"{jobs.unfinished_tasks} disclosures to go.")


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
    added = []

    def write(record: dict) -> None:
        append_record(record)
        added.append(record["disclosure_index"])

    log(f"Fetching disclosures published from {args.start:%d.%m.%Y} to {args.end:%d.%m.%Y}, newest first. "
        "Ctrl+C to stop.")
    try:
        fetch_range(args.start, args.end, written_indexes(), write)
        log("Done.")
    except KeyboardInterrupt:
        stop_writing()
        log("Stopping. Run the same command again to continue.")
    log(f"Added {len(added)} disclosures to {JSONL_PATH}.")


if __name__ == "__main__":
    main()
