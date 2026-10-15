"""Fetch the KAP disclosure list.

Only disclosures that concern a company in companies.txt are kept: as soon as a list arrives, the
others are dropped, so their pages are never requested.
"""
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel
from tenacity import retry, stop_after_attempt, wait_exponential

from kap_http import BLOCKED_STATUSES, RATE_LIMIT_PAUSE, throttle

API_URL = "https://www.kap.org.tr/tr/api/disclosure/list/main"
DISCLOSURE_URL = "https://www.kap.org.tr/tr/Bildirim/{}"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/140.0 Safari/537.36",
    "Accept-Language": "tr",
    "Referer": "https://www.kap.org.tr/tr",
}
DATE_FORMAT = "%d.%m.%Y"
WATCHLIST_PATH = Path(__file__).parent / "companies.txt"


def load_watchlist() -> set[str]:
    """Stock codes in companies.txt: the first word of each line that is neither empty nor a # comment."""
    lines = WATCHLIST_PATH.read_text(encoding="utf-8").splitlines()
    return {line.split()[0] for line in lines if line.strip() and not line.startswith("#")}


WATCHLIST = load_watchlist()

class Disclosure(BaseModel):
    disclosureIndex: int
    disclosureId: str
    publishDate: str
    stockCode: str | None
    companyTitle: str | None
    title: str | None
    summary: str | None
    disclosureClass: str | None
    disclosureType: str | None
    disclosureCategory: str | None
    period: str | None
    year: int | None
    attachmentCount: int | None
    isLate: bool | None
    relatedStocks: str | None
    mkkMemberOid: str | None
    relatedDisclosureOid: str | None
    isChanged: str | None
    hasMultiLanguageSupport: str | None
    donem: int | None

    @property
    def published(self) -> datetime:
        return datetime.strptime(self.publishDate, "%d.%m.%Y %H:%M:%S")


def list_fields(d: Disclosure) -> dict:
    """The disclosure's fields from the list, named as in its record."""
    return {
        "disclosure_index": d.disclosureIndex,
        "disclosure_id": d.disclosureId,
        "publish_date": d.published,
        "stock_code": d.stockCode,
        "company_title": d.companyTitle,
        "title": d.title,
        "summary": d.summary,
        "disclosure_class": d.disclosureClass,
        "disclosure_type": d.disclosureType,
        "disclosure_category": d.disclosureCategory,
        "period": d.period,
        "year": d.year,
        "attachment_count": d.attachmentCount,
        "is_late": d.isLate,
        "related_stocks": d.relatedStocks,
        "url": DISCLOSURE_URL.format(d.disclosureIndex),
        "mkk_member_oid": d.mkkMemberOid,  # KAP's id of the publishing member; unlike its stock code it never changes
        "related_disclosure_oid": d.relatedDisclosureOid,  # disclosure_id of the earlier disclosure this one continues, updates or corrects
        "is_changed": d.isChanged,  # 'DUZENLENEN' (updated) or 'DUZELTILEN' (corrected); None for an original
        "has_multi_language_support": d.hasMultiLanguageSupport,  # 'Y' when the disclosure also has an English version
        "term": d.donem,  # KAP's 'donem', e.g. the quarter of a financial report
    }


def stock_codes(text: str | None) -> set[str]:
    return {code.strip() for code in (text or "").split(",") if code.strip()}  # e.g. "ISATR, ISBTR, ISCTR"


@retry(stop=stop_after_attempt(5), wait=wait_exponential(min=2, max=30), reraise=True)
def fetch_list(client: httpx.Client, start: date, end: date) -> list[Disclosure]:
    """Return the disclosures published from `start` to `end` (both included) that concern a company in
    the watchlist, as its own disclosure or as one of its related stocks. While KAP blocks, the retries
    wait for the pause of every thread."""
    throttle.wait()
    try:
        r = client.post(API_URL, json={"fromDate": start.strftime(DATE_FORMAT), "toDate": end.strftime(DATE_FORMAT),
                                       "memberTypes": ["IGS", "DDK"]})
    except httpx.RemoteProtocolError:  # dropped before any answer: KAP's firewall blocks
        throttle.pause(RATE_LIMIT_PAUSE, "connection dropped")
        raise
    if r.status_code in BLOCKED_STATUSES:
        throttle.pause(RATE_LIMIT_PAUSE, f"HTTP {r.status_code}")
    r.raise_for_status()
    disclosures = [Disclosure(**item["disclosureBasic"]) for item in r.json()]
    return [d for d in disclosures if (stock_codes(d.stockCode) | stock_codes(d.relatedStocks)) & WATCHLIST]


def months(start: date, end: date) -> list[tuple[date, date]]:
    """Split `start`..`end` into calendar-month ranges, newest first. KAP lists a whole month in one response."""
    ranges = []
    while end >= start:
        first = max(start, end.replace(day=1))
        ranges.append((first, end))
        end = first - timedelta(days=1)
    return ranges
