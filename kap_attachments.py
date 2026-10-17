"""Extract the text and tables of disclosure attachments, and decide which attachments are worth reading.

Text comes from the PDF text layer, or OCR for scanned pages and images. Tables come from the text layer
(PyMuPDF table finder) or, on scans, from ruled grid lines whose cells are OCR'd one by one.
Files are processed in memory and never saved. Every attachment of every disclosure is read, except the
English version of a document that also comes in Turkish.
"""
import io
import os
import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytesseract
from dotenv import load_dotenv
from PIL import Image, UnidentifiedImageError

from kap_http import file_size

load_dotenv(Path(__file__).parent / ".env")
pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]

OCR_LANG = "tur"
OCR_DPI = 300
MIN_TEXT_CHARS = 50  # a page with less text than this is treated as scanned
MAX_OCR_PAGES = 50  # OCR takes ~6 s per page; pages beyond this are counted as skipped
MIN_CELL_SIZE = 20  # px at OCR_DPI; smaller enclosed gaps are line noise, not cells
MIN_TABLE_CELLS = 4  # fewer enclosed cells is a framed box or underline, not a table
ROW_TOLERANCE = 15  # px; cells whose tops are this close belong to the same row

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


def unwrap(data: bytes) -> bytes:
    """KAP wraps files in a serialized Java byte[]: 23-byte header, 4-byte length, then the file."""
    if (size := file_size(data)) is not None:
        return data[27:27 + size]
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


ENGLISH_TAGS = {"en", "eng", "english", "ingilizce"}  # e.g. 'BİGTK (ENG).pdf', 'RAPORU_KAP EN.pdf'
TURKISH_TAGS = {"tr", "tur", "turkce"}
# Words of file names, folded to ASCII. A name with more English than Turkish ones is taken as English.
ENGLISH_WORDS = {"investor", "presentation", "release", "earning", "earnings", "results", "report", "relations",
                 "quarter", "financial", "statements", "announcement", "press", "bulletin", "traffic", "rating",
                 "valuation", "translation", "amendment", "articles", "annual", "interim", "january", "february",
                 "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"}
TURKISH_WORDS = {"yatirimci", "sunum", "sunumu", "bilgilendirme", "bulten", "bulteni", "sonuc", "sonuclar",
                 "sonuclari", "notu", "dokuman", "dokumani", "rapor", "raporu", "ceyrek", "donem", "donemi",
                 "finansal", "faaliyet", "trafik", "aciklama", "degerleme", "derecelendirme", "basin", "duyuru",
                 "ocak", "subat", "mart", "nisan", "mayis", "haziran", "temmuz", "agustos", "eylul", "ekim",
                 "kasim", "aralik"}


def name_words(file_name: str) -> list[str]:
    """'GWIND_Yatırımcı_Sunumu.pdf' -> ['gwind', 'yatirimci', 'sunumu']"""
    stem = file_name.rsplit(".", 1)[0].replace("ı", "i").replace("İ", "I")
    return re.findall(r"[a-z0-9]+", unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode().lower())


def is_english(words: list[str]) -> bool:
    # 'en' is also a Turkish word, so as a tag it only counts at the end of the name.
    if (ENGLISH_TAGS - {"en"}) & set(words) or words[-1:] == ["en"]:
        return True
    if TURKISH_TAGS & set(words):
        return False
    return len(ENGLISH_WORDS & set(words)) > len(TURKISH_WORDS & set(words))


def to_read(file_names: dict[str, str]) -> set[str]:
    """The file ids worth reading among one disclosure's attachments ({file_id: file_name}): all but the
    English version of a document that also comes in Turkish. The language is guessed from file names; when
    unsure, the attachment is read. If every attachment looks English, all are read, as there is no other copy."""
    words = {file_id: name_words(name) for file_id, name in file_names.items()}
    joined = {file_id: "".join(w) for file_id, w in words.items()}
    english = {file_id for file_id in file_names if is_english(words[file_id]) or any(  # e.g. 'Vkfyo07102026e.pdf'
        joined[file_id] in (joined[other] + "e", joined[other] + "en") for other in file_names if other != file_id)}
    return set(file_names) - english if len(english) < len(file_names) else set(file_names)
