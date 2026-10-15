"""Where the data goes: one JSON Lines file, data/disclosures.jsonl, with every disclosure on its own line,
for training a model.

A disclosure is written once its page and all of its attachments have been read, so the file holds only
complete records, and the disclosures already in it are the ones a run skips.
"""
import json
import re
import threading
from itertools import groupby
from operator import itemgetter
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
JSONL_PATH = DATA_DIR / "disclosures.jsonl"

STATEMENT_KEYS = ("statement", "currency", "consolidation")
ITEM_KEYS = ("concept", "label", "member", "period_label", "period_start", "period_end", "value")
TEXT_KEYS = ("file_type", "page_count", "ocr_pages", "skipped_pages", "text")
SKIPPED = {"file_type": "skipped", "page_count": 0, "ocr_pages": 0, "skipped_pages": 0, "text": "", "tables": []}
INDEX_RE = re.compile(r'\{"disclosure_index": (\d+)')  # the first key of every line

_write_lock = threading.Lock()


def disclosure_record(listed: dict, page: dict, financial_items: list[dict], texts: dict[str, dict]) -> dict:
    """One disclosure as plain data: `listed`, its fields from the list, then everything read from its page,
    financial statements and attachments. `texts` maps the file id of each attachment that was read to
    kap_attachments.extract()'s result; the other attachments are recorded as skipped. Dates stay date objects."""
    # Statements in name order, as in the records written before; items in page order.
    items = sorted(financial_items, key=lambda i: (i["statement"] is None, i["statement"] or "", i["row_no"]))
    statements = [{**dict(zip(STATEMENT_KEYS, key)), "items": [{k: item[k] for k in ITEM_KEYS} for item in group]}
                  for key, group in groupby(items, key=itemgetter(*STATEMENT_KEYS))]
    attachments = []
    for file_id, a in sorted(page["attachments"].items(), key=lambda pair: pair[1]["file_name"]):
        result = texts.get(file_id, SKIPPED)
        attachments.append({"file_id": file_id, "file_name": a["file_name"], "url": a["url"],
                            **{k: result[k] for k in TEXT_KEYS},
                            "tables": [{"page": page_no, "method": method, "rows": rows}
                                       for page_no, method, rows in result["tables"] if rows]})
    # A financial report's fields are its statements, so they are left out to avoid storing them twice.
    return listed | {"page_format": page["page_format"], "fields": [] if financial_items else page["fields"],
                     "tables": page["tables"], "financial_statements": statements, "attachments": attachments,
                     "text": page["text"]}


def written_indexes() -> set[int]:
    """disclosure_index of every disclosure already in the JSON Lines file."""
    if not JSONL_PATH.exists():
        return set()
    with JSONL_PATH.open(encoding="utf-8") as f:
        return {int(INDEX_RE.match(line).group(1)) for line in f}


def as_json(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, default=str)  # dates as text, e.g. '2026-10-08 09:15:00'


def append_record(record: dict) -> None:
    """Add a disclosure to the JSON Lines file. Safe to call from several threads."""
    line = as_json(record) + "\n"
    with _write_lock, JSONL_PATH.open("a", encoding="utf-8") as f:
        f.write(line)


def stop_writing() -> None:
    """Let a record being written finish and keep any other from starting, so the process can exit without
    leaving half a line behind. Call it only on the way out: later writes wait forever."""
    _write_lock.acquire()
