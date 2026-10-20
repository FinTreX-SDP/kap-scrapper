"""OCR the scanned pages that kap_attachments.MAX_OCR_PAGES left out, for the attachments listed in S3 at
reocr/todo.json, and write each page's text and tables to reocr/results-<PART>.jsonl in S3. A one-off AWS
job (see aws/reocr.sh): PARTS tasks each take every PARTS-th attachment, starting at PART. merge_reocr.py
then puts the pages into the disclosure records.

The pages left out are found as extract_pdf() found them: past the first MAX_OCR_PAGES pages with less
than MIN_TEXT_CHARS of text and an image. The results are uploaded every UPLOAD_INTERVAL seconds, and a
task started again skips the pages already in them. Each page is read in a process of its own, so one
that crashes Tesseract or MuPDF is logged and left out instead of stopping the task.

Usage:
    BUCKET=my-bucket PART=0 PARTS=3 python kap_reocr.py
"""
import json
import os
import queue
import signal
import tempfile
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

import boto3
import httpx
import pymupdf
from botocore.exceptions import ClientError
from PIL import Image

from kap_attachments import MAX_OCR_PAGES, MIN_TEXT_CHARS, OCR_DPI, clean_text, ocr_page, unwrap
from kap_disclosures import HEADERS
from kap_http import ATTACHMENT_TIMEOUT, kap_get
from kap_watch import log

PROCESSES = os.cpu_count() or 1
UPLOAD_INTERVAL = 300  # seconds
TODO_KEY = "reocr/todo.json"


def stop(signum, frame) -> None:
    raise KeyboardInterrupt  # ECS stops a task with SIGTERM; stop as on Ctrl+C, so the results are uploaded


def left_out(path: str) -> list[int]:
    """Numbers (from 1) of the scanned pages extract_pdf() did not OCR."""
    scanned = []
    with pymupdf.open(path) as doc:
        for page in doc:
            if len(clean_text(page.get_text())) < MIN_TEXT_CHARS and page.get_images():
                scanned.append(page.number + 1)
    return scanned[MAX_OCR_PAGES:]


def ocr_one(path: str, page_no: int) -> dict:
    """OCR one page as extract_pdf() does; run in a worker process."""
    with pymupdf.open(path) as doc:
        pix = doc[page_no - 1].get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY)
    text, tables = ocr_page(Image.frombytes("L", (pix.width, pix.height), pix.samples))
    return {"page": page_no, "text": text, "tables": [rows for rows in tables if rows]}


def main() -> None:
    bucket = os.environ["BUCKET"]
    part, parts = int(os.environ.get("PART", 0)), int(os.environ.get("PARTS", 1))
    results_key = f"reocr/results-{part}.jsonl"
    s3 = boto3.client("s3")
    signal.signal(signal.SIGTERM, stop)
    todo = json.loads(s3.get_object(Bucket=bucket, Key=TODO_KEY)["Body"].read())[part::parts]
    results = Path(tempfile.gettempdir()) / "results.jsonl"
    try:
        s3.download_file(bucket, results_key, str(results))
    except ClientError as e:
        if e.response["Error"]["Code"] != "404":
            raise
        results.write_text("")
    done = {(r["file_id"], r["page"]) for r in map(json.loads, results.read_text(encoding="utf-8").splitlines())}
    lock = threading.Lock()

    def upload() -> None:
        with lock:
            s3.upload_file(str(results), bucket, results_key)

    finished = threading.Event()

    def upload_regularly() -> None:
        while not finished.wait(UPLOAD_INTERVAL):
            try:
                upload()
            except Exception as e:
                log(f"Upload to S3 failed: {e!r}")

    threading.Thread(target=upload_regularly, daemon=True).start()
    folder = Path(tempfile.mkdtemp())
    jobs, mismatched = [], []
    try:
        with httpx.Client(headers=HEADERS, http2=True, timeout=ATTACHMENT_TIMEOUT) as client:
            for n, a in enumerate(todo, 1):
                path = folder / f"{a['file_id']}.pdf"
                path.write_bytes(unwrap(kap_get(client, a["url"])))
                pages = left_out(str(path))
                if len(pages) != a["skipped_pages"]:
                    mismatched.append(a["disclosure_index"])
                    log(f"  {a['disclosure_index']} '{a['file_name']}': {len(pages)} pages left out, "
                        f"the record says {a['skipped_pages']}")
                jobs += [(a, str(path), p) for p in pages if (a["file_id"], p) not in done]
                if n % 20 == 0:
                    log(f"Downloaded {n} of {len(todo)} files.")
        log(f"{len(jobs)} pages to OCR in {PROCESSES} processes ({len(done)} done before); "
            f"{len(mismatched)} files differ from their records.")
        pending, failed, written = queue.Queue(), [], [0]
        for job in jobs:
            pending.put(job)

        def work() -> None:
            # A process of its own per thread: a page that crashes it is the one it was given.
            process = ProcessPoolExecutor(1)
            while True:
                try:
                    a, path, p = pending.get_nowait()
                except queue.Empty:
                    break
                try:
                    page = process.submit(ocr_one, path, p).result()
                except Exception as e:
                    failed.append((a["disclosure_index"], p, repr(e)))
                    if isinstance(e, BrokenProcessPool):
                        process = ProcessPoolExecutor(1)
                    continue
                line = json.dumps({"disclosure_index": a["disclosure_index"], "file_id": a["file_id"], **page},
                                  ensure_ascii=False)
                with lock, results.open("a", encoding="utf-8", errors="replace") as f:
                    f.write(line + "\n")
                    written[0] += 1
                    if written[0] % 200 == 0:
                        log(f"{written[0]} of {len(jobs)} pages OCR'd.")
            process.shutdown()

        workers = [threading.Thread(target=work, daemon=True) for _ in range(PROCESSES)]
        for worker in workers:
            worker.start()
        while any(worker.is_alive() for worker in workers):
            for worker in workers:
                worker.join(1)  # short, so SIGTERM is noticed
        log(f"Done. Failed pages: {failed}" if failed else "Done: every page left out is OCR'd.")
    except KeyboardInterrupt:
        log("Stopping. Start the task again to continue.")
    finally:
        finished.set()
        upload()


if __name__ == "__main__":
    main()
