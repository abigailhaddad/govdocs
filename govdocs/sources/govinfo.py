"""govinfo.py — GPO's own published sitemaps, and the PDFs behind them.

govinfo is the largest federal document corpus reachable without a credential.
The bulk paths take no key at all: `api.govinfo.gov` is a keyed convenience
layer over the same content and is not used here. robots.txt is served, returns
200, disallows only `/search/`, `/core/`, `/profiles/` and the user and admin
paths, declares no crawl delay, and publishes about forty per-collection
sitemap indexes for exactly this purpose. Nothing here needs a browser.

Three URL shapes, all verified:

    /sitemap/{COLL}_sitemap_index.xml     -> per-year sitemaps
    /sitemap/{COLL}_{YEAR}_sitemap.xml    -> /app/details/{PACKAGE_ID} links
    /content/pkg/{PKG}/pdf/{PKG}.pdf      -> the PDF as GPO published it

A package's title and publication date come from `/metadata/pkg/{PKG}/mods.xml`,
which robots permits and which runs a few kilobytes. The sitemaps carry only a
`lastmod`, and that is when GPO last touched the record rather than when the
document was issued -- a 2001 GAO report has a 2012 lastmod -- so it is not used
as a date.

Collections are chosen, not swept. `USCOURTS` alone is 2.17M packages and would
drown everything else, and `BILLSTATUS`, `BILLSUM` and `HOB` are metadata and
summaries rather than documents. The default set below is the documentary
material: hearings, committee reports and prints, congressional documents, GAO
reports, agency publications and presidential documents. Widen it by passing
`collections=` rather than by editing this list.

`GAOREPORTS` here stops at 2008. GAO's own site carries 2009 onward and is a
separate problem: it 403s plain requests and needs a browser.
"""

from __future__ import annotations

import html
import re
import time
from typing import Iterator

import requests

BASE = "https://www.govinfo.gov"
USER_AGENT = ("govdocs/0.1 (federal document archive; "
              "contact: abigail.haddad@gmail.com)")

DEFAULT_COLLECTIONS = ("CHRG", "CRPT", "CDOC", "CPRT", "GAOREPORTS", "GOVPUB", "DCPD")

# One agency spelling per collection, since the package ID does not carry one.
# GOVPUB is deliberately blank: it spans the whole government and guessing from
# a package ID would file documents under the wrong department silently.
COLLECTION_AGENCY = {
    "CHRG": "Congress", "CRPT": "Congress", "CDOC": "Congress",
    "CPRT": "Congress", "GAOREPORTS": "GAO", "DCPD": "Executive Office of the President",
    "GOVPUB": "", "FR": "", "SERIALSET": "Congress", "USCOURTS": "",
}

COLLECTION_KIND = {
    "CHRG": "Congressional hearing", "CRPT": "Committee report",
    "CDOC": "Congressional document", "CPRT": "Committee print",
    "GAOREPORTS": "GAO report", "GOVPUB": "Government publication",
    "DCPD": "Presidential document",
}

LOC_RE = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", re.I)
YEAR_RE = re.compile(r"_(\d{4})_sitemap\.xml$", re.I)
PKG_RE = re.compile(r"/app/details/([^/?#]+)")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
DATE_RE = re.compile(r"<dateIssued[^>]*>\s*(\d{4}(?:-\d{2}){0,2})", re.I)
UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s))).strip()


class GovInfo:
    name = "govinfo"
    collection = "govinfo"

    def __init__(self, max_calls: int = 200,
                 collections: tuple[str, ...] = DEFAULT_COLLECTIONS,
                 delay: float = 0.2):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.max_calls = max_calls
        self.collections = tuple(collections)
        self.delay = delay
        self.calls = 0

    def _get(self, url: str, timeout: int = 60) -> requests.Response:
        if self.calls:
            time.sleep(self.delay)   # no crawl-delay in robots.txt; be polite anyway
        r = self.session.get(url, timeout=timeout)
        r.raise_for_status()
        return r

    def _year_sitemaps(self, coll: str, since_year: int) -> list[str]:
        """Year sitemaps for a collection, newest first, older than `since` dropped."""
        self.calls += 1
        try:
            xml = self._get(f"{BASE}/sitemap/{coll}_sitemap_index.xml").text
        except Exception:
            return []
        dated = []
        for url in LOC_RE.findall(xml):
            m = YEAR_RE.search(url)
            # A sitemap with no year in its name is kept: dropping it would
            # silently lose a collection that does not file by year.
            year = int(m.group(1)) if m else 9999
            if year >= since_year:
                dated.append((year, url))
        return [u for _, u in sorted(dated, reverse=True)]

    def discover(self, since: str, until: str | None = None,
                 limit: int | None = None) -> Iterator[dict]:
        try:
            since_year = int(str(since)[:4])
        except ValueError:
            since_year = 0
        n = 0
        seen: set[str] = set()
        for coll in self.collections:
            for sm in self._year_sitemaps(coll, since_year):
                if self.calls >= self.max_calls:
                    return
                self.calls += 1
                try:
                    xml = self._get(sm).text
                except Exception:
                    continue
                for loc in LOC_RE.findall(xml):
                    m = PKG_RE.search(loc)
                    if not m:
                        continue
                    pkg = html.unescape(m.group(1))
                    if pkg in seen:
                        continue
                    seen.add(pkg)
                    # Title and date are left to fetch(): collect.py skips
                    # already-collected keys before calling it, so a rerun does
                    # not spend one mods.xml request per document it already has.
                    yield {
                        "source": "govinfo",
                        "notice_id": UNSAFE_RE.sub("-", pkg)[:120],
                        "index": 0,
                        "url": f"{BASE}/content/pkg/{pkg}/pdf/{pkg}.pdf",
                        "landing_url": loc,
                        "title": "",
                        "date": "",
                        "agency": COLLECTION_AGENCY.get(coll, ""),
                        "office": coll,
                        "notice_type": COLLECTION_KIND.get(coll, f"govinfo {coll}"),
                        "package_id": pkg,
                    }
                    n += 1
                    if limit and n >= limit:
                        return

    def _mods(self, rec: dict) -> None:
        """Fill in title and issue date from the package's MODS record."""
        try:
            xml = self._get(f"{BASE}/metadata/pkg/{rec['package_id']}/mods.xml",
                            timeout=60).text
        except Exception:
            return
        # The package's own title and date come first; granule records that
        # follow describe the bills and witnesses inside it, not the document.
        t = TITLE_RE.search(xml)
        if t:
            rec["title"] = _clean(t.group(1))[:300]
        d = DATE_RE.search(xml)
        if d:
            rec["date"] = d.group(1)

    def fetch(self, rec: dict) -> tuple[bytes, str]:
        self._mods(rec)
        r = self._get(rec["url"], timeout=180)
        return r.content, f"{rec['package_id']}.pdf"
