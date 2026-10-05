"""Turn the XBRL-tagged financial statements on KAP financial report pages into numeric, period-based rows.

Reads the disclosure HTML already stored by kap_details.py or kap_watch.py; sends no requests to KAP.
kap_watch.py also calls this right after it saves a new financial report.

Usage:
    python kap_financials.py         # stored reports that have no rows yet
    python kap_financials.py --all   # re-parse every stored report (after a parser change)
"""
import argparse
import re
from datetime import date, datetime

import duckdb
import polars as pl
from selectolax.lexbor import LexborHTMLParser, LexborNode

from kap_output import DB_PATH, export_to_excel, write_detail_file

SCHEMA = """
CREATE TABLE IF NOT EXISTS financial_items (
    disclosure_index INTEGER,
    statement VARCHAR,        -- e.g. 'Finansal Durum Tablosu (Bilanço)'
    row_no INTEGER,           -- position within the statement, keeps the original order
    concept VARCHAR,          -- XBRL concept, e.g. 'ifrs-full_Revenue'
    label VARCHAR,
    member VARCHAR,           -- equity component in the statement of changes in equity, else NULL
    period_label VARCHAR,     -- e.g. 'Cari Dönem', 'Önceki Dönem 3 Aylık'
    period_start DATE,        -- NULL for a point in time, e.g. a balance sheet date
    period_end DATE,
    value DOUBLE,
    currency VARCHAR,
    consolidation VARCHAR     -- 'Konsolide' or 'Konsolide Olmayan'
);
"""
ROW_SCHEMA = {"disclosure_index": pl.Int32, "statement": pl.String, "row_no": pl.Int32, "concept": pl.String,
              "label": pl.String, "member": pl.String, "period_label": pl.String, "period_start": pl.Date,
              "period_end": pl.Date, "value": pl.Float64, "currency": pl.String, "consolidation": pl.String}

PERIOD_RE = re.compile(r"^(.*?)\s*(\d{2}\.\d{2}\.\d{4})(?:\s*-\s*(\d{2}\.\d{2}\.\d{4}))?$")
# Turkish format 1.234.567,89; thousands groups must have 3 digits, so dates like 01.09.2026 do not match.
NUMBER_RE = re.compile(r"^\(?-?(\d{1,3}(\.\d{3})+|\d+)(,\d+)?\)?$")
VALUE_COL_RE = re.compile(r"col-order-class-(\d+)")
MEMBER_COL_RE = re.compile(r"col-index-(\d+)")
TOTAL_COL_RE = re.compile(r"col-abstract-index-(\d+)")


def text_tr(node: LexborNode) -> str:
    """Turkish text of a cell; bilingual cells hold a Turkish and an English version."""
    tr = node.css_first(".content-tr")
    return " ".join((tr or node).text().split())


def child_tds(row: LexborNode) -> list[LexborNode]:
    return [c for c in row.iter() if c.tag == "td"]


def css_class(node: LexborNode) -> str:
    return node.attributes.get("class") or ""


def parse_period(text: str) -> tuple[str, date | None, date] | None:
    """'Cari Dönem01.01.2026 - 30.06.2026' -> ('Cari Dönem', 2026-01-01, 2026-06-30)."""
    m = PERIOD_RE.match(text)
    if not m:
        return None
    label, first, second = m.groups()
    to_date = lambda s: datetime.strptime(s, "%d.%m.%Y").date()  # noqa: E731
    return (label, to_date(first), to_date(second)) if second else (label, None, to_date(first))


def parse_number(text: str) -> float | None:
    if not NUMBER_RE.match(text):
        return None
    value = float(text.strip("()").replace(".", "").replace(",", "."))
    return -value if text.startswith("(") else value


def header_value(tree: LexborNode, title: str) -> str | None:
    """Value next to a report header cell such as 'Sunum Para Birimi'."""
    for cell in tree.css("td.financial-header-title"):
        if cell.text(strip=True) == title:
            sib = cell.next
            while sib is not None and sib.tag != "td":
                sib = sib.next
            return text_tr(sib) if sib else None
    return None


def equity_members(table: LexborNode) -> dict[int, str]:
    """Column number -> equity component, read from the dimensional header of the changes-in-equity table."""
    members = {}
    for cell in table.css("td.taxonomy-dimensional-header-cell"):
        if m := MEMBER_COL_RE.search(css_class(cell)):
            members[int(m.group(1))] = text_tr(cell)
    # A group's total column is an empty cell placed right after the group's name cell.
    for cell in table.css("td.taxonomy-dimensional-header-abstract-cell"):
        name = cell.prev
        while name is not None and name.tag != "td":
            name = name.prev
        if (m := TOTAL_COL_RE.search(css_class(cell))) and name is not None:
            members[int(m.group(1))] = text_tr(name)
    return members


def parse_statement(table: LexborNode) -> list[dict]:
    title = table.css_first(".taxonomy-abstract-title")
    statement = text_tr(title) if title else None
    # Ordinary statements: one period per value column, in header order.
    periods = [parse_period(text_tr(c)) for c in table.css("td.context-header")]
    # Changes in equity: columns are equity components, and periods are row groups.
    members = {} if periods else equity_members(table)
    items, current_period = [], None
    for row in table.css("tr"):
        tds = child_tds(row)
        if not tds:
            continue
        if "taxonomy-field-name-cell" not in css_class(tds[0]):
            for td in tds:  # a row-group header such as 'Önceki Dönem01.01.2025 - 30.06.2025'
                if period := parse_period(text_tr(td)):
                    current_period = period
            continue
        concept = " ".join(tds[0].text().split()).split("|")[0]
        title_cell = next((td for td in tds if "taxonomy-field-title" in css_class(td)), None)
        label = text_tr(title_cell) if title_cell else None
        values = [td for td in tds if "taxonomy-context-value" in css_class(td)]
        for position, cell in enumerate(values):
            value = parse_number(" ".join(cell.text().split()))
            if value is None:
                continue
            if periods:
                period, member = periods[position] if position < len(periods) else None, None
            else:
                m = VALUE_COL_RE.search(css_class(cell))
                period, member = current_period, members.get(int(m.group(1))) if m else None
            period_label, period_start, period_end = period or (None, None, None)
            items.append({"statement": statement, "row_no": len(items), "concept": concept, "label": label,
                          "member": member, "period_label": period_label, "period_start": period_start,
                          "period_end": period_end, "value": value})
    return items


def parse_financials(html: str) -> list[dict]:
    tree = LexborHTMLParser(html)
    currency = header_value(tree, "Sunum Para Birimi")
    consolidation = header_value(tree, "Finansal Tablo Niteliği")
    return [{**item, "currency": currency, "consolidation": consolidation}
            for table in tree.css("table.financial-table") for item in parse_statement(table)]


def save(con: duckdb.DuckDBPyConnection, disclosure_index: int, items: list[dict]) -> None:
    con.begin()  # all or nothing; replaces earlier rows of the same report
    try:
        con.execute("DELETE FROM financial_items WHERE disclosure_index = ?", [disclosure_index])
        if items:
            df = pl.DataFrame([{"disclosure_index": disclosure_index, **i} for i in items], schema=ROW_SCHEMA)
            con.execute("INSERT INTO financial_items BY NAME SELECT * FROM df")
        con.commit()
    except Exception:
        con.rollback()
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract financial statements from stored KAP reports.")
    parser.add_argument("--all", action="store_true", help="re-parse every stored report")
    args = parser.parse_args()

    con = duckdb.connect(str(DB_PATH))
    con.execute(SCHEMA)
    # Other forms reuse the same table layout, so only financial report disclosures (class FR) are read.
    query = """
        SELECT x.disclosure_index, x.html FROM disclosure_details x JOIN disclosures d USING (disclosure_index)
        WHERE d.disclosure_class = 'FR' AND x.html LIKE '%financial-table%'
    """
    if not args.all:
        query += " AND x.disclosure_index NOT IN (SELECT disclosure_index FROM financial_items)"
    for disclosure_index, html in con.execute(query).fetchall():
        items = parse_financials(html)
        save(con, disclosure_index, items)
        write_detail_file(con, disclosure_index)
        print(f"{disclosure_index}: {len(items)} values")
    export_to_excel(con)
    con.close()


if __name__ == "__main__":
    main()
