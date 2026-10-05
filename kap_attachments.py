"""Download disclosure attachments and extract their text and tables.

Text comes from the PDF text layer, or OCR for scanned pages and images. Tables come from the text layer
(PyMuPDF table finder) or, on scans, from ruled grid lines whose cells are OCR'd one by one.
Run kap_details.py first; it lists the attachments. Files are processed in memory and never saved.

Usage:
    python kap_attachments.py              # all attachments without extracted text
    python kap_attachments.py --limit 20   # only 20 (most recent disclosures first)
"""
import argparse
import io
import os
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import cv2
import duckdb
import httpx
import numpy as np
import polars as pl
import pymupdf
import pytesseract
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError
from tqdm import tqdm

from kap_disclosures import HEADERS, REQUEST_DELAY
from kap_http import RateLimited, kap_get
from kap_output import DB_PATH, export_to_excel, write_detail_file

load_dotenv(Path(__file__).parent / ".env")
pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]

OCR_LANG = "tur"
OCR_DPI = 300
MIN_TEXT_CHARS = 50  # a page with less text than this is treated as scanned
MAX_OCR_PAGES = 50  # OCR takes ~6 s per page; pages beyond this are counted as skipped
MIN_CELL_SIZE = 20  # px at OCR_DPI; smaller enclosed gaps are line noise, not cells
MIN_TABLE_CELLS = 4  # fewer enclosed cells is a framed box or underline, not a table
ROW_TOLERANCE = 15  # px; cells whose tops are this close belong to the same row

SCHEMA = """
CREATE TABLE IF NOT EXISTS attachment_texts (
    disclosure_index INTEGER,
    file_id VARCHAR,
    file_type VARCHAR,        -- 'pdf', an image format such as 'png', or 'unsupported'
    page_count INTEGER,
    ocr_pages INTEGER,        -- pages read with OCR
    skipped_pages INTEGER,    -- scanned pages not read because of MAX_OCR_PAGES
    text VARCHAR,
    extracted_at TIMESTAMP,
    PRIMARY KEY (disclosure_index, file_id)
);
CREATE TABLE IF NOT EXISTS attachment_tables (
    disclosure_index INTEGER,
    file_id VARCHAR,
    page INTEGER,
    table_no INTEGER,         -- numbered across the whole file
    row_no INTEGER,
    method VARCHAR,           -- 'text' (PDF text layer) or 'ocr' (grid cells OCR'd one by one)
    cells VARCHAR[]
);
"""


def clean_text(text: str) -> str:
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def ocr(image: Image.Image) -> str:
    return clean_text(pytesseract.image_to_string(image, lang=OCR_LANG))


def clean_cell(value: str | None) -> str:
    return " ".join((value or "").split())


def find_grid_tables(img: np.ndarray) -> list[list[tuple[int, int, int, int]]]:
    """Find ruled tables in a grayscale page image and return each table's cell boxes (x, y, w, h)."""
    height, width = img.shape
    binary = cv2.adaptiveThreshold(~img, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY, 15, -2)
    h_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (width // 30, 1)))
    v_lines = cv2.morphologyEx(binary, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, height // 60)))
    grid = cv2.dilate(h_lines | v_lines, np.ones((3, 3), np.uint8))
    # Each connected set of lines is one table; the gaps the lines enclose are its cells.
    _, grid_labels = cv2.connectedComponents(grid)
    n, _, stats, _ = cv2.connectedComponentsWithStats(~grid, connectivity=4)
    tables = defaultdict(list)
    for i in range(1, n):
        x, y, w, h, _ = stats[i]
        if w < MIN_CELL_SIZE or h < MIN_CELL_SIZE or x == 0 or y == 0 or x + w >= width or y + h >= height:
            continue  # line noise, or the page background around the tables
        # The table is the line set around the cell; look at a margin, as scans are slightly skewed.
        around = grid_labels[y - 3:y + h + 3, x - 3:x + w + 3]
        line_ids = around[around > 0]
        if line_ids.size:
            tables[np.bincount(line_ids).argmax()].append((x, y, w, h))
    return [cells for cells in tables.values() if len(cells) >= MIN_TABLE_CELLS]


def ocr_table(img: np.ndarray, cells: list[tuple[int, int, int, int]]) -> list[list[str]]:
    """OCR each cell separately so no value can drift into another row or column."""
    rows = []  # (top y, [(x, text), ...])
    for x, y, w, h in sorted(cells, key=lambda c: (c[1], c[0])):
        crop = Image.fromarray(img[y + 2:y + h - 2, x + 2:x + w - 2])
        text = clean_cell(pytesseract.image_to_string(crop, lang=OCR_LANG, config="--psm 6"))
        if rows and abs(rows[-1][0] - y) < ROW_TOLERANCE:
            rows[-1][1].append((x, text))
        else:
            rows.append((y, [(x, text)]))
    return [[text for _, text in sorted(row)] for _, row in rows]


def ocr_page(image: Image.Image) -> tuple[str, list[list[list[str]]]]:
    """OCR a grayscale page: full text, plus each ruled table read cell by cell."""
    img = np.array(image)
    return ocr(image), [ocr_table(img, cells) for cells in find_grid_tables(img)]


def extract_pdf(data: bytes) -> dict:
    parts, tables, ocr_pages, skipped = [], [], 0, 0
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for page in doc:
            page_no = page.number + 1
            text = clean_text(page.get_text())
            if len(text) < MIN_TEXT_CHARS and page.get_images():
                if ocr_pages < MAX_OCR_PAGES:
                    pix = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csGRAY)
                    text, page_tables = ocr_page(Image.frombytes("L", (pix.width, pix.height), pix.samples))
                    tables += [(page_no, "ocr", rows) for rows in page_tables]
                    ocr_pages += 1
                else:
                    skipped += 1
            else:
                tables += [(page_no, "text", [[clean_cell(c) for c in row] for row in t.extract()])
                           for t in page.find_tables().tables]
            parts.append(f"[Page {page_no}]\n{text}")
        page_count = doc.page_count
    return {"file_type": "pdf", "page_count": page_count, "ocr_pages": ocr_pages,
            "skipped_pages": skipped, "text": "\n\n".join(parts), "tables": tables}


JAVA_BYTE_ARRAY_HEADER = b"\xac\xed\x00\x05ur\x00\x02[B"  # Java-serialized byte[] prefix


def unwrap(data: bytes) -> bytes:
    """KAP wraps files in a serialized Java byte[]: 23-byte header, 4-byte length, then the file."""
    if data.startswith(JAVA_BYTE_ARRAY_HEADER):
        length = int.from_bytes(data[23:27], "big")
        return data[27:27 + length]
    return data


def extract(data: bytes) -> dict:
    data = unwrap(data)
    if data[:4] == b"%PDF":
        return extract_pdf(data)
    try:
        image = Image.open(io.BytesIO(data))
    except UnidentifiedImageError:
        return {"file_type": "unsupported", "page_count": 0, "ocr_pages": 0, "skipped_pages": 0,
                "text": "", "tables": []}
    text, page_tables = ocr_page(image.convert("L"))
    return {"file_type": image.format.lower(), "page_count": 1, "ocr_pages": 1, "skipped_pages": 0,
            "text": text, "tables": [(1, "ocr", rows) for rows in page_tables]}


TABLE_ROW_SCHEMA = {"disclosure_index": pl.Int32, "file_id": pl.String, "page": pl.Int32, "table_no": pl.Int32,
                    "row_no": pl.Int32, "method": pl.String, "cells": pl.List(pl.String)}


def save(con: duckdb.DuckDBPyConnection, disclosure_index: int, file_id: str, result: dict) -> None:
    con.begin()  # all or nothing, so a failed save never leaves partial rows behind
    try:
        table_rows = [
            {"disclosure_index": disclosure_index, "file_id": file_id, "page": page, "table_no": table_no,
             "row_no": row_no, "method": method, "cells": cells}
            for table_no, (page, method, rows) in enumerate(result["tables"])
            for row_no, cells in enumerate(rows)
        ]
        if table_rows:
            df = pl.DataFrame(table_rows, schema=TABLE_ROW_SCHEMA)
            con.execute("INSERT INTO attachment_tables BY NAME SELECT * FROM df")
        con.execute("INSERT INTO attachment_texts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [disclosure_index, file_id, result["file_type"], result["page_count"],
                     result["ocr_pages"], result["skipped_pages"], result["text"], datetime.now()])
        con.commit()
    except Exception:
        con.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract text from KAP disclosure attachments.")
    parser.add_argument("--limit", type=int, help="process at most this many attachments")
    args = parser.parse_args()

    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA)
    query = """
        SELECT a.disclosure_index, a.file_id, a.url FROM disclosure_attachments a
        WHERE NOT EXISTS (SELECT 1 FROM attachment_texts t
                          WHERE t.disclosure_index = a.disclosure_index AND t.file_id = a.file_id)
        ORDER BY a.disclosure_index DESC
    """
    if args.limit:
        query += f" LIMIT {args.limit}"
    todo = con.execute(query).fetchall()

    done, failed = 0, []
    try:
        with httpx.Client(headers=HEADERS, http2=True, timeout=120) as client:
            for disclosure_index, file_id, url in tqdm(todo, desc="Attachments"):
                try:
                    save(con, disclosure_index, file_id, extract(kap_get(client, url)))
                    write_detail_file(con, disclosure_index)
                    done += 1
                except RateLimited:
                    raise
                except Exception as e:  # keep going on long runs; failures are retried next run
                    failed.append(file_id)
                    tqdm.write(f"Failed {disclosure_index}/{file_id}: {e}")
                time.sleep(REQUEST_DELAY)
    except RateLimited:
        print("KAP is still rate limiting after several pauses. Stopping; run again later to continue.")
    finally:  # export whatever was saved, even after Ctrl+C
        export_to_excel(con)
        con.close()
    print(f"Processed attachments: {done}, failed: {len(failed)}, remaining: {len(todo) - done - len(failed)}")


if __name__ == "__main__":
    main()
