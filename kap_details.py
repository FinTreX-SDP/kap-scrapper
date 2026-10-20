"""Fetch a disclosure's detail page and parse its content: text, labeled fields, tables and attachment links."""
import httpx
from selectolax.lexbor import LexborHTMLParser, LexborNode

from kap_disclosures import DISCLOSURE_URL
from kap_http import kap_get

ATTACHMENT_SELECTOR = 'a[href*="/api/file/download/"]'


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
                fields.append({"section": section, "concept": None, "label": section, "value": value})
        elif node.tag == "table":
            tables.append({"section": section, "rows": [[clean(td) for td in tr.css("td")] for tr in node.css("tr")]})
        elif "txtWhite" in (node.attributes.get("class") or ""):
            section = clean(node)
        else:
            fields.append({"section": section, "concept": None, "label": clean(node), "value": clean(next_td(node))})
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
        for cell in cells:  # one per column, e.g. current and previous period
            value = cell.text(separator="\n", strip=True) if "summernote" in cell.attributes.get("class", "") else clean(cell)
            if value:
                fields.append({"section": section, "concept": concept, "label": label or section, "value": value})
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


def fetch_page(client: httpx.Client, disclosure_index: int) -> str:
    return kap_get(client, DISCLOSURE_URL.format(disclosure_index)).decode("utf-8")
