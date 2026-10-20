"""What every scraper does with a disclosure: fetch its page, financial statements and attachments, and
make its record. kap_watch.py, kap_history.py and kap_aws.py share it.
"""
import os
import queue
import tempfile
import threading
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from datetime import datetime

import httpx
import pymupdf

import kap_attachments
import kap_details
import kap_financials
from kap_disclosures import HEADERS, Disclosure, list_fields
from kap_http import ATTACHMENT_TIMEOUT, connection_options, kap_get
from kap_output import disclosure_record, unreadable

# Downloading an attachment takes minutes (KAP sends ~10 KB/s per connection) but little memory; reading it
# takes seconds of CPU and most of the memory. So however many workers download, only this many read at once.
# A scanned document holds its slot for minutes of OCR, so there are enough slots for small files to pass.
EXTRACT_SLOTS = 4
extracting = threading.BoundedSemaphore(EXTRACT_SLOTS)


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


def extract_by_page(data: bytes) -> dict:
    """A PDF read one page at a time in a process of its own. A page whose tables crash the reader is read
    again without them, and one that crashes it even so is left empty; both are noted in the result."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(data)
    try:
        with pymupdf.open(f.name) as doc:
            page_count = doc.page_count
        pages, table_errors, unread = [], [], []
        process = ProcessPoolExecutor(1)
        for n in range(1, page_count + 1):
            page = None
            for tables in (True, False):
                try:
                    page = process.submit(kap_attachments.read_file_page, f.name, n, tables).result()
                    break
                except BrokenProcessPool:
                    process = ProcessPoolExecutor(1)
            pages.append(page)
            if not tables:  # read without tables, or not at all
                table_errors.append(n)
            if page is None:
                unread.append(n)
        process.shutdown()
    finally:
        os.remove(f.name)
    return kap_attachments.pdf_result(pages) | {"table_errors": table_errors, "unread_pages": unread}


def extract_apart(data: bytes) -> dict:
    """kap_attachments.extract() in a process of its own. Some PDF pages crash PyMuPDF's table finder, which
    Python cannot catch (on 19.10.2026 one killed a scraper twice at the same place); this way only that
    process dies, and the file is read again page by page. All memory a file took goes back when its
    process ends."""
    try:
        with ProcessPoolExecutor(1) as process:
            return process.submit(kap_attachments.extract, data).result()
    except BrokenProcessPool:
        data = kap_attachments.unwrap(data)
        if data[:4] != b"%PDF":
            raise
        return extract_by_page(data)


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
                texts[file_id] = result = extract_apart(data)
        except Exception as e:  # a file that cannot be read would fail every time; record it as such instead
            texts[file_id] = result = unreadable(e)
            log(f"  attachment {d.disclosureIndex} '{a['file_name']}' could not be read: {e!r}")
            continue
        notes = f", tables not read on pages {result['table_errors']}" if result.get("table_errors") else ""
        log(f"  attachment {d.disclosureIndex} '{a['file_name']}': {result['page_count']} pages, "
            f"{result['ocr_pages']} OCR, {len(result['tables'])} tables{notes}")
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
