"""governmentattic.py — a volunteer archive of documents obtained under FOIA.

Every document on the site was obtained under FOIA or a state sunshine law, so
unlike an agency reading room nothing here is filtered by what the statute
obliges an agency to post. It is small next to DocumentCloud but denser: the
whole site is releases.

A 1990s frameset. Two category indexes point at fourteen listing pages whose
rows read

    <a href="...pdf">Title</a> - [PDF 364 KB - 20-Oct-2025]

robots.txt disallows only /images/, /Slush/ and /test/, none of which we touch,
and asks for Crawl-delay: 5, which is why the rate here is 0.2/sec and should
not be raised.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterator

import requests

BASE = "https://www.governmentattic.org"
CATEGORY_INDEXES = (f"{BASE}/DocumentsCat.html", f"{BASE}/FOIALogsCat.html")
USER_AGENT = ("govdocs/0.1 (federal document archive; "
              "contact: abigail.haddad@gmail.com)")

CATEGORY_RE = re.compile(r'href="(https?://(?:www\.)?governmentattic\.org/[^"]*\.html)"', re.I)
NAV_RE = re.compile(r"/(Frame-\d|Ack|links|DocumentsCat|FOIALogsCat)\.html$", re.I)
ROW_RE = re.compile(
    r'<a\s+href="([^"]+\.pdf)"[^>]*>(.*?)</a>\s*(?:</?p\d>\s*)*-?\s*\[PDF\s*([^\]]*)\]',
    re.I | re.S)
BARE_RE = re.compile(r'<a\s+href="([^"]+\.pdf)"[^>]*>(.*?)</a>', re.I | re.S)
TAGS = re.compile(r"<[^>]+>")
AGENCY_RE = re.compile(r"\b(DOJ|FBI|DEA|ATF|BOP|EOUSA|USMS|OIG|CIA|NSA|DHS|DOD|DOE|EPA|"
                       r"HHS|HUD|NASA|NRC|ODNI|SEC|USAID|VA|FDIC|USCIS|DOS|NSF|FRA)\b")


# A row's size-and-date marker can be swept into the next row's anchor text by
# the fallback pattern, giving titles like "- [PDF 14.4 MB - 15-Sep-2025] DoD..."
LEADING_META_RE = re.compile(r"^\s*-?\s*\[PDF[^\]]*\]\s*")


def _clean(s: str) -> str:
    t = re.sub(r"\s+", " ", TAGS.sub("", s)).strip()
    return LEADING_META_RE.sub("", t).strip(" -")


def _iso(meta: str) -> str:
    m = re.search(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})", meta)
    if not m:
        return ""
    try:
        return datetime.strptime("-".join(m.groups()), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return ""


class GovernmentAttic:
    name = "governmentattic"
    collection = "foia"

    def __init__(self, max_calls: int = 40):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.max_calls = max_calls
        self.calls = 0

    def _get(self, url: str) -> str:
        import time
        if self.calls:
            time.sleep(5.0)          # robots.txt asks for Crawl-delay: 5
        self.calls += 1
        r = self.session.get(url, timeout=60)
        r.raise_for_status()
        return r.content.decode("latin-1", "replace")

    def _categories(self) -> list[str]:
        pages: list[str] = []
        for idx in CATEGORY_INDEXES:
            try:
                html = self._get(idx)
            except Exception:
                continue
            for url in CATEGORY_RE.findall(html):
                if not NAV_RE.search(url) and url not in pages:
                    pages.append(url)
        return pages

    def discover(self, since: str, until: str | None = None,
                 limit: int | None = None) -> Iterator[dict]:
        n = 0
        seen: set[str] = set()
        for cat in self._categories():
            if self.calls >= self.max_calls:
                return
            try:
                html = self._get(cat)
            except Exception:
                continue
            rows = ROW_RE.findall(html)
            got = {u for u, _, _ in rows}
            rows += [(u, t, "") for u, t in BARE_RE.findall(html) if u not in got]
            for url, title, meta in rows:
                if url in seen:
                    continue
                seen.add(url)
                title = _clean(title)
                am = AGENCY_RE.search(url.rsplit("/", 1)[-1]) or AGENCY_RE.search(title)
                yield {
                    "source": "governmentattic",
                    "notice_id": url.rsplit("/", 1)[-1].removesuffix(".pdf")[:120],
                    "index": 0,
                    "url": url,
                    "landing_url": cat,
                    "title": title[:300],
                    "date": _iso(meta),
                    "agency": am.group(1) if am else "",
                    "office": cat.rsplit("/", 1)[-1],
                    "notice_type": "FOIA release (GovernmentAttic)",
                }
                n += 1
                if limit and n >= limit:
                    return

    def fetch(self, rec: dict) -> tuple[bytes, str]:
        import time
        time.sleep(5.0)
        r = self.session.get(rec["url"], timeout=180)
        r.raise_for_status()
        return r.content, rec["url"].rsplit("/", 1)[-1]
