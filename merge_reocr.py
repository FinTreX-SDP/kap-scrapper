"""Put the pages kap_reocr.py OCR'd into the disclosure records: each page's text in place of its empty
'[Page N]' part, its tables among the attachment's tables in page order, and ocr_pages and skipped_pages
counted again. The attachments kap_salvage.py read again (salvage.jsonl) replace their 'error' records.
Other lines are written back unchanged. Prints what it changed and what it could not.

Usage:
    python merge_reocr.py data/aws data/reocr      # records folder, folder with results-*.jsonl, salvage.jsonl
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# A page's part, up to the next page: extract_pdf() joins them with an empty line and page texts have none.
PAGE_RE = re.compile(r"\[Page (\d+)\]\n(.*?)(?=\n\n\[Page \d+\]\n|\Z)", re.S)
FILE_ID_RE = re.compile(r'"file_id": "([0-9a-f]+)"')


def merge(attachment: dict, pages: dict[int, dict]) -> int:
    """Put `pages` ({page number: kap_reocr result}) into `attachment`; return how many it used."""
    used = 0

    def put(match: re.Match) -> str:
        nonlocal used
        if page := pages.get(int(match.group(1))):
            used += 1
            return f"[Page {page['page']}]\n{page['text']}"
        return match.group(0)

    attachment["text"] = PAGE_RE.sub(put, attachment["text"])
    tables = attachment["tables"] + [{"page": p["page"], "method": "ocr", "rows": rows}
                                     for p in pages.values() for rows in p["tables"]]
    attachment["tables"] = sorted(tables, key=lambda t: t["page"])  # stable, so a page keeps its order
    attachment["ocr_pages"] += used
    attachment["skipped_pages"] -= used
    return used


def replace(attachment: dict, result: dict) -> None:
    """Put a kap_salvage.py result in place of an attachment recorded as an error."""
    attachment.pop("error", None)
    attachment.update({k: result[k] for k in ("file_type", "page_count", "ocr_pages", "skipped_pages", "text")},
                      tables=[{"page": page, "method": method, "rows": rows} for page, method, rows in result["tables"]
                              if rows],
                      table_errors=result["table_errors"])


def main() -> None:
    records_dir, results_dir = map(Path, sys.argv[1:3])
    pages: dict[str, dict[int, dict]] = defaultdict(dict)
    for path in results_dir.glob("results-*.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            pages[r["file_id"]][r["page"]] = r
    salvaged = {}
    if (salvage := results_dir / "salvage.jsonl").exists():
        salvaged = {r["file_id"]: r["result"] for r in map(json.loads, salvage.read_text(encoding="utf-8").splitlines())}
    print(f"{sum(map(len, pages.values()))} pages of {len(pages)} attachments in the results; "
          f"{len(salvaged)} attachments read again.")
    merged, unused, replaced = defaultdict(int), dict(pages) | dict(salvaged), set()
    for path in sorted(records_dir.glob("*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        for n, line in enumerate(lines):
            if not set(FILE_ID_RE.findall(line)) & (pages.keys() | salvaged.keys()):
                continue
            record = json.loads(line)
            for a in record["attachments"]:
                if a["file_id"] in pages:
                    merged[a["file_id"]] += merge(a, pages[a["file_id"]])
                    unused.pop(a["file_id"], None)
                if a["file_id"] in salvaged and a["file_type"] == "error":
                    replace(a, salvaged[a["file_id"]])
                    replaced.add(a["file_id"])
                    unused.pop(a["file_id"], None)
            lines[n] = json.dumps(record, ensure_ascii=False) + "\n"
        path.write_text("".join(lines), encoding="utf-8", errors="replace", newline="")  # keep \n line ends
    print(f"Merged {sum(merged.values())} pages into {len(merged)} attachments; replaced {len(replaced)} errors.")
    short = {f: (merged[f], len(pages[f])) for f in merged if merged[f] != len(pages[f])}
    if short:
        print(f"Pages not placed (attachment: placed, OCR'd): {short}")
    if unused:
        print(f"Attachments in the results but not in the records: {list(unused)}")


if __name__ == "__main__":
    main()
