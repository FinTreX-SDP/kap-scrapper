"""Fetch the KAP disclosures of the companies in companies.txt published in a date range and write each to
data/disclosures.jsonl once its page, financial statements and attachments are read.

kap_watch.py catches new disclosures live; this script fetches the past. Run one of them at a time, as
both write the same file. Lists are fetched a month at a time, as KAP returns a whole month in one
response, and their disclosures are fetched newest first by WORKERS threads. Disclosures already in the
file are skipped, so running the same command again continues where it stopped. What fails is tried
again in a few more rounds at the end (see fetch_range). Stop with Ctrl+C.

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
from kap_http import connection_options, gate
from kap_output import DATA_DIR, JSONL_PATH, append_record, stop_writing, written_indexes
from kap_watch import fetch_disclosure, log, start_workers

# The workers overlap KAP's slow answers: a page takes 0.4-9 s to come, an attachment several minutes
# at ~10 KB/s per connection.
# KAP kept 10-30 KB/s on each of 12 connections at once without a stall (~230 KB/s in total), which is
# about all the line takes. While some workers wait for attachments, the others go on with pages.
WORKERS = 12
PROGRESS_INTERVAL = 60  # seconds between progress lines
# After the first pass the lists are fetched again and what is still missing, a disclosure or a whole month
# that failed, is tried again, up to RETRY_ROUNDS times, RETRY_WAIT apart: longer than a block lasts.
RETRY_ROUNDS = 3
RETRY_WAIT = 300  # seconds


def fetch_range(start: date, end: date, skip: set[int], write: Callable[[dict], None],
                proxies: list[str] | None = None, workers: int = WORKERS) -> set[int]:
    """Fetch the disclosures published from `start` to `end` whose disclosure_index is not in `skip`, and pass
    each record to `write`, from the worker threads, once it is complete; each written one is added to `skip`.
    What fails is tried again in up to RETRY_ROUNDS more rounds. Returns the disclosure_index of the listed
    disclosures still missing, empty when the range is complete (a list that never came is logged).
    Every request goes through the next of `proxies`, if given, and so from a new IP each time, so a block
    is retried at once instead of waited out."""
    if proxies:
        gate.enabled = False
    jobs: queue.Queue = queue.Queue()
    listed: set[int] = set()
    with httpx.Client(headers=HEADERS, timeout=60, **connection_options(proxies)) as client:  # pages and lists; they are small

        def handle(file_client: httpx.Client, d: Disclosure) -> None:
            try:
                write(fetch_disclosure(client, file_client, d))
                skip.add(d.disclosureIndex)
            except Exception as e:
                log(f"  {d.disclosureIndex} failed, will be tried again: {e!r}")

        def queue_missing() -> int:
            """Fetch the lists and queue what is not written yet; return how many lists failed."""
            failed_lists = 0
            for first, last in months(start, end):
                try:
                    month = fetch_list(client, first, last)
                except Exception as e:
                    log(f"List of {first:%d.%m.%Y}-{last:%d.%m.%Y} failed, will be tried again: {e!r}")
                    failed_lists += 1
                    continue
                listed.update(d.disclosureIndex for d in month)
                todo = sorted((d for d in month if d.disclosureIndex not in skip), key=lambda d: d.published, reverse=True)
                log(f"{first:%d.%m.%Y}-{last:%d.%m.%Y}: {len(month)} disclosures of the watched companies, "
                    f"{len(todo)} to fetch.")
                for d in todo:
                    jobs.put(d)
            return failed_lists

        def wait_for_jobs() -> None:
            last_progress = time.monotonic()
            while jobs.unfinished_tasks:
                time.sleep(1)  # short, so Ctrl+C is noticed
                if time.monotonic() - last_progress > PROGRESS_INTERVAL:
                    last_progress = time.monotonic()
                    log(f"{jobs.unfinished_tasks} disclosures to go.")

        start_workers(workers, jobs, handle, proxies)
        for round_no in range(RETRY_ROUNDS + 1):
            if round_no:
                log(f"{len(listed - skip)} disclosures and {failed_lists} lists are missing; trying again in "
                    f"{RETRY_WAIT // 60} minutes (round {round_no} of {RETRY_ROUNDS}).")
                time.sleep(RETRY_WAIT)
            failed_lists = queue_missing()
            wait_for_jobs()
            if not failed_lists and not listed - skip:
                break
    missing = listed - skip
    if missing or failed_lists:
        log(f"Incomplete: {len(missing)} disclosures and {failed_lists} lists still missing: {sorted(missing)}")
    else:
        log(f"Complete: all {len(listed)} disclosures listed from {start:%d.%m.%Y} to {end:%d.%m.%Y} are written.")
    return missing


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
