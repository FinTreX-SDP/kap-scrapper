"""Fetch detail pages for disclosures already in the database and parse their content.

Run kap_disclosures.py first. This script fetches every disclosure that has no details yet.

Usage:
    python kap_details.py              # all disclosures without details
    python kap_details.py --limit 20   # only the 20 most recent ones (for testing)
"""
import argparse
import json
import time
from datetime import datetime

import duckdb
import httpx
import polars as pl
from selectolax.lexbor import LexborHTMLParser, LexborNode
from tqdm import tqdm

from kap_disclosures import DISCLOSURE_URL, HEADERS, REQUEST_DELAY
from kap_http import RateLimited, kap_get
from kap_output import DB_PATH, export_to_excel, write_detail_file

ATTACHMENT_SELECTOR = 'a[href*="/api/file/download/"]'

SCHEMA = """
CREATE TABLE IF NOT EXISTS disclosure_details (
    disclosure_index INTEGER PRIMARY KEY,
    page_format VARCHAR,      -- 'xbrl', 'legacy' or 'empty'
    text VARCHAR,             -- plain Turkish text of the disclosure body
    html VARCHAR,             -- raw HTML of the disclosure body, kept for re-parsing
    fetched_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS disclosure_fields (
    disclosure_index INTEGER,
    row_no INTEGER,
    section VARCHAR,
    concept VARCHAR,          -- XBRL concept name, NULL on legacy pages
    label VARCHAR,
    value_col INTEGER,        -- column position, e.g. current vs previous period
    value VARCHAR
);
CREATE TABLE IF NOT EXISTS disclosure_tables (
    disclosure_index INTEGER,
    table_no INTEGER,
    section VARCHAR,
    rows_json VARCHAR         -- list of rows, each a list of cell texts
);
CREATE TABLE IF NOT EXISTS disclosure_attachments (
    disclosure_index INTEGER,
    file_id VARCHAR,
    file_name VARCHAR,
    url VARCHAR,
    PRIMARY KEY (disclosure_index, file_id)
);
"""


def clean(node: LexborNode | None) -> str:
    return " ".join(node.text(separator=" ").split()) if node else ""


def next_td(node: LexborNode) -> LexborNode | None:
    """Return the <td> that follows the <td> containing `node`."""
    td = node.parent
    while td is not None and td.tag != "td":
        td = td.parent
    sib = td.next if td else None
    while sib is not None and sib.tag != "td":
        sib = sib.next
    return sib


def is_free_text(node: LexborNode) -> bool:
    """A control-label alone in its row (not a label's value, not a grid cell) is a free-text block."""
    td = node.parent
    if td is None or td.tag != "td" or len([c for c in td.parent.iter() if c.tag == "td"]) != 1:
        return False
    parent = td.parent
    while parent is not None:
        if parent.tag == "table" and parent.attributes.get("border") == "1":
            return False
        parent = parent.parent
    return True


def parse_legacy(area: LexborNode) -> tuple[list[dict], list[dict]]:
    fields, tables, section = [], [], None
    for node in area.css('.txtWhite, div.bold.font14, table[border="1"], div.control-label'):
        if "control-label" in (node.attributes.get("class") or ""):
            if is_free_text(node) and (value := node.text(separator="\n", strip=True)):
                fields.append({"row_no": len(fields), "section": section, "concept": None,
                               "label": section, "value_col": 0, "value": value})
        elif node.tag == "table":
            rows = [[clean(td) for td in tr.css("td")] for tr in node.css("tr")]
            tables.append({"table_no": len(tables), "section": section, "rows_json": json.dumps(rows, ensure_ascii=False)})
        elif "txtWhite" in (node.attributes.get("class") or ""):
            section = clean(node)
        else:
            fields.append({"row_no": len(fields), "section": section, "concept": None,
                           "label": clean(node), "value_col": 0, "value": clean(next_td(node))})
    return fields, tables


def parse_xbrl(area: LexborNode) -> list[dict]:
    fields, section = [], None
    for name in area.css(".taxonomy-field-name"):
        row = name.parent.parent  # div -> td -> tr
        concept = clean(name).split("|")[0]
        label = clean(row.css_first(".taxonomy-field-title .content-tr"))
        cells = [c for c in row.css("td.taxonomy-context-value, td.taxonomy-context-value-summernote")
                 if "content-en" not in c.attributes.get("class", "")]
        if concept.endswith("Abstract") or not cells:
            section = label
            continue
        for col, cell in enumerate(cells):
            value = cell.text(separator="\n", strip=True) if "summernote" in cell.attributes.get("class", "") else clean(cell)
            if value:
                fields.append({"row_no": len(fields), "section": section, "concept": concept,
                               "label": label or section, "value_col": col, "value": value})
    return fields


def parse_body(area: LexborNode) -> dict:
    """Parse the disclosure body. Note: strips English/helper nodes from `area` in place."""
    raw_html = area.html
    if area.css_first(".taxonomy-field-name"):
        page_format, fields, tables = "xbrl", parse_xbrl(area), []
    else:
        page_format, (fields, tables) = "legacy", parse_legacy(area)

    # Plain text: drop English duplicates, XBRL concept names and help icons.
    for node in area.css(".content-en, .taxonomy-field-name, .taxonomy-field-documentation"):
        node.decompose()
    lines = [" ".join(line.split()) for line in area.text(separator="\n").splitlines()]
    text = "\n".join(line for line in lines if line)
    return {"page_format": page_format, "text": text, "html": raw_html, "fields": fields, "tables": tables}


def parse_page(html: str) -> dict:
    tree = LexborHTMLParser(html)
    attachments = {}
    for a in tree.css(ATTACHMENT_SELECTOR):
        url = a.attributes["href"]
        attachments[url.rsplit("/", 1)[-1]] = {"file_name": a.text(strip=True), "url": url}

    area = tree.css_first(".disclosureScrollableArea")
    if area is None:
        return {"page_format": "empty", "text": "", "html": "", "fields": [], "tables": [], "attachments": attachments}
    return {**parse_body(area), "attachments": attachments}


def reparse_stored(con: duckdb.DuckDBPyConnection) -> int:
    """Re-run the parser on HTML already in the database. No requests are sent to KAP."""
    stored = con.execute("SELECT disclosure_index, html FROM disclosure_details WHERE html <> ''").fetchall()
    for disclosure_index, html in tqdm(stored, desc="Re-parsing"):
        area = LexborHTMLParser(html).css_first(".disclosureScrollableArea")
        page = parse_body(area)
        con.begin()
        try:
            for table in ("disclosure_fields", "disclosure_tables"):
                con.execute(f"DELETE FROM {table} WHERE disclosure_index = ?", [disclosure_index])
            insert_rows(con, "disclosure_fields", disclosure_index, page["fields"])
            insert_rows(con, "disclosure_tables", disclosure_index, page["tables"])
            con.execute("UPDATE disclosure_details SET page_format = ?, text = ? WHERE disclosure_index = ?",
                        [page["page_format"], page["text"], disclosure_index])
            con.commit()
        except Exception:
            con.rollback()
            raise
        write_detail_file(con, disclosure_index)
    return len(stored)


def fetch_page(client: httpx.Client, disclosure_index: int) -> str:
    return kap_get(client, DISCLOSURE_URL.format(disclosure_index)).decode("utf-8")


def insert_rows(con: duckdb.DuckDBPyConnection, table: str, disclosure_index: int, rows: list[dict]) -> None:
    if rows:
        df = pl.DataFrame([{"disclosure_index": disclosure_index, **r} for r in rows])
        con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM df")


def save(con: duckdb.DuckDBPyConnection, disclosure_index: int, page: dict) -> None:
    con.begin()  # all or nothing, so a failed save never leaves partial rows behind
    try:
        insert_rows(con, "disclosure_fields", disclosure_index, page["fields"])
        insert_rows(con, "disclosure_tables", disclosure_index, page["tables"])
        insert_rows(con, "disclosure_attachments", disclosure_index,
                    [{"file_id": fid, **a} for fid, a in page["attachments"].items()])
        con.execute("INSERT INTO disclosure_details VALUES (?, ?, ?, ?, ?)",
                    [disclosure_index, page["page_format"], page["text"], page["html"], datetime.now()])
        con.commit()
    except Exception:
        con.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch KAP disclosure detail pages.")
    parser.add_argument("--limit", type=int, help="fetch at most this many (most recent first)")
    parser.add_argument("--reparse", action="store_true",
                        help="re-parse pages already in the database instead of fetching new ones")
    args = parser.parse_args()

    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA)
    if args.reparse:
        count = reparse_stored(con)
        export_to_excel(con)
        con.close()
        print(f"Re-parsed {count} stored pages")
        return
    query = """
        SELECT disclosure_index FROM disclosures
        WHERE disclosure_index NOT IN (SELECT disclosure_index FROM disclosure_details)
        ORDER BY publish_date DESC
    """
    if args.limit:
        query += f" LIMIT {args.limit}"
    todo = [row[0] for row in con.execute(query).fetchall()]

    done, failed = 0, []
    try:
        with httpx.Client(headers=HEADERS, http2=True, timeout=60) as client:
            for disclosure_index in tqdm(todo, desc="Details"):
                try:
                    save(con, disclosure_index, parse_page(fetch_page(client, disclosure_index)))
                    write_detail_file(con, disclosure_index)
                    done += 1
                except RateLimited:
                    raise
                except Exception as e:  # keep going on long runs; failures are retried next run
                    failed.append(disclosure_index)
                    tqdm.write(f"Failed {disclosure_index}: {e}")
                time.sleep(REQUEST_DELAY)
    except RateLimited:
        print("KAP is still rate limiting after several pauses. Stopping; run again later to continue.")
    finally:  # export whatever was saved, even after Ctrl+C
        total = export_to_excel(con)
        con.close()
    print(f"Fetched details: {done}, failed: {len(failed)}, remaining: {len(todo) - done - len(failed)}")
    print(f"Disclosures in database: {total}")


if __name__ == "__main__":
    main()
