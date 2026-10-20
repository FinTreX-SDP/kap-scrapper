"""Read again, page by page, the attachments recorded as file_type 'error' because a page crashed PyMuPDF's
table finder (on 20.10.2026 all seven did; text extraction never crashed). Each page is read in a process of
its own: if finding its tables crashes, the page is read once more without them. Writes, per attachment,
a result like kap_attachments.extract()'s, plus 'table_errors', the pages whose tables could not be read,
to salvage/results.jsonl in S3. Unlike extract_pdf(), every scanned page is OCR'd (no MAX_OCR_PAGES), as
kap_reocr.py did for the others. A one-off AWS task (see aws/salvage.sh); merge_reocr.py merges it.

Usage:
    BUCKET=my-bucket python kap_salvage.py
"""
import json
import os
import queue
import tempfile
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import boto3
import httpx
import pymupdf
from PIL import Image

from kap_attachments import MIN_TEXT_CHARS, OCR_DPI, clean_cell, clean_text, ocr_page, unwrap
from kap_disclosures import HEADERS
from kap_http import ATTACHMENT_TIMEOUT, kap_get
from kap_watch import log

PROCESSES = os.cpu_count() or 1
TODO_KEY, RESULTS_KEY = "salvage/todo.json", "salvage/results.jsonl"


def read_page(path: str, page_no: int, tables: bool) -> dict:
    """One page as extract_pdf() reads it; run in a worker process."""
    with pymupdf.open(path) as doc:
        page = doc[page_no - 1]
        text = clean_text(page.get_text())
        if len(text) < MIN_TEXT_CHARS and page.get_images():
            pix = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY)
            text, found = ocr_page(Image.frombytes("L", (pix.width, pix.height), pix.samples))
            return {"page": page_no, "ocr": True, "text": text,
                    "tables": [("ocr", rows) for rows in found] if tables else []}
        found = page.find_tables().tables if tables else []
        return {"page": page_no, "ocr": False, "text": text,
                "tables": [("text", [[clean_cell(c) for c in row] for row in t.extract()]) for t in found]}


def main() -> None:
    bucket = os.environ["BUCKET"]
    s3 = boto3.client("s3")
    todo = json.loads(s3.get_object(Bucket=bucket, Key=TODO_KEY)["Body"].read())
    folder = Path(tempfile.mkdtemp())
    pending, pages, lock = queue.Queue(), {}, threading.Lock()
    with httpx.Client(headers=HEADERS, http2=True, timeout=ATTACHMENT_TIMEOUT) as client:
        for a in todo:
            path = folder / f"{a['file_id']}.pdf"
            path.write_bytes(unwrap(kap_get(client, a["url"])))
            with pymupdf.open(path) as doc:
                a["page_count"] = doc.page_count
            for p in range(1, a["page_count"] + 1):
                pending.put((a["file_id"], str(path), p))
    log(f"{pending.qsize()} pages of {len(todo)} files to read in {PROCESSES} processes.")

    def work() -> None:
        process = ProcessPoolExecutor(1)
        while True:
            try:
                file_id, path, p = pending.get_nowait()
            except queue.Empty:
                break
            result = None
            for tables in (True, False):  # without tables if finding them crashes the process
                try:
                    result = process.submit(read_page, path, p, tables).result()
                    result["table_error"] = not tables
                    break
                except BrokenProcessPool:
                    process = ProcessPoolExecutor(1)
                    log(f"  {file_id} page {p} crashed{' finding tables' if tables else ''}.")
            with lock:
                pages[(file_id, p)] = result
        process.shutdown()

    workers = [threading.Thread(target=work) for _ in range(PROCESSES)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    lines = []
    for a in todo:
        read = [pages[(a["file_id"], p)] for p in range(1, a["page_count"] + 1)]
        result = {
            "file_type": "pdf", "page_count": a["page_count"], "ocr_pages": sum(1 for r in read if r and r["ocr"]),
            "skipped_pages": 0,
            "text": "\n\n".join(f"[Page {p}]\n{r['text'] if r else ''}" for p, r in enumerate(read, 1)),
            "tables": [(r["page"], method, rows) for r in read if r for method, rows in r["tables"]],
            "table_errors": [p for p, r in enumerate(read, 1) if not r or r["table_error"]],
            "unread_pages": [p for p, r in enumerate(read, 1) if not r],
        }
        log(f"  {a['disclosure_index']} '{a['file_name']}': {result['page_count']} pages, {result['ocr_pages']} OCR, "
            f"{len(result['tables'])} tables, tables not read on pages {result['table_errors']}, "
            f"unread pages {result['unread_pages']}")
        lines.append(json.dumps({"disclosure_index": a["disclosure_index"], "file_id": a["file_id"], "result": result},
                                ensure_ascii=False))
    s3.put_object(Bucket=bucket, Key=RESULTS_KEY, Body=("\n".join(lines) + "\n").encode("utf-8", "replace"))
    log("Done.")


if __name__ == "__main__":
    main()
