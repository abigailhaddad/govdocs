"""oversight.py — every federal Inspector General, from one API.

oversight.gov is the government-wide clearing house for IG work, and its
api/v2/reports endpoint hands back the whole recent feed as JSON: report title,
submitting OIG, component agency, publication date, report number, product type
and a direct document URL. One call covered 47 distinct OIGs -- FDIC, Treasury,
Labor, SBA, HHS, TIGTA, the pandemic-recovery special IG and the rest.

The endpoint ignores every filter. page, offset, items_per_page and field
filters all return the same 1,063 rows -- spanning 2020 to 2026, so it is a
fixed sample rather than a recent window. 841 of those carry a document URL.

The sitemap has 38,183 report pages, 36 times what the API admits to, so that is
what this walks. The API is still read first because its rows come with
structured metadata -- submitting OIG, component agency, report number, product
type -- that a report page only half exposes.

Report pages often link a document hosted by the originating audit office rather
than by oversight.gov, which is how state and local IG work reaches the archive
alongside federal.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterator

API = "https://www.oversight.gov/api/v2/reports"
SITEMAP = "https://www.oversight.gov/sitemap.xml"
SITEMAP_CACHE = Path("data/oversight_reports.json")
SITEMAP_REFRESH_DAYS = 14
LOC_RE = re.compile(r"<loc>([^<]+)</loc>")
PDF_RE = re.compile(r'href="([^"]+\.pdf[^"]*)"', re.I)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)
USER_AGENT = ("govdocs/0.1 (federal document archive; "
              "contact: abigail.haddad@gmail.com)")


class Oversight:
    name = "oversight"
    collection = "foia"

    def __init__(self, max_calls: int = 2000):
        self.max_calls = max_calls
        self.calls = 0

    def _get(self) -> list[dict]:
        self.calls += 1
        req = urllib.request.Request(API, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=180) as resp:
            data = json.loads(resp.read())
        # Rows arrive either bare or wrapped in a "node" key depending on the
        # query; take whichever is there.
        return [x.get("node", x) for x in data.get("nodes", [])]

    def _report_pages(self) -> list[str]:
        """Every report page in the sitemap, cached.

        38,183 of them against the API's 1,063. Walking 24 sub-sitemaps takes a
        minute, so the list is kept for a fortnight.
        """
        if SITEMAP_CACHE.exists():
            age = (time.time() - SITEMAP_CACHE.stat().st_mtime) / 86400
            if age < SITEMAP_REFRESH_DAYS:
                return json.loads(SITEMAP_CACHE.read_text())
        out: list[str] = []
        try:
            index = self._fetch_text(SITEMAP)
        except Exception:
            return out
        for sub in LOC_RE.findall(index):
            try:
                xml = self._fetch_text(sub)
            except Exception:
                continue
            out += [u for u in LOC_RE.findall(xml) if "/reports/" in u]
        if out:
            SITEMAP_CACHE.parent.mkdir(parents=True, exist_ok=True)
            SITEMAP_CACHE.write_text(json.dumps(sorted(set(out)), indent=0))
        return sorted(set(out))

    def _fetch_text(self, url: str) -> str:
        self.calls += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read().decode("utf-8", "replace")

    def discover_sitemap(self, seen: set[str], limit: int | None = None
                         ) -> Iterator[dict]:
        """Documents reached by walking report pages."""
        n = 0
        for page_url in self._report_pages():
            if self.calls >= self.max_calls:
                return
            if page_url in seen:
                continue
            try:
                html = self._fetch_text(page_url)
            except Exception:
                continue
            m = PDF_RE.search(html)
            if not m:
                continue
            t = TITLE_RE.search(html)
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", t.group(1))).strip() if t else ""
            title = title.split("|")[0].strip()
            # Report pages link documents both absolutely (a state audit
            # office's own site) and relatively (oversight.gov's own files).
            url = urllib.parse.urljoin(page_url, m.group(1))
            yield {
                "source": "oversight",
                "notice_id": str(abs(hash(page_url)) % (10 ** 12)),
                "index": 0,
                "url": url,
                "landing_url": page_url,
                "title": title[:300],
                "date": "",
                "agency": "",
                "office": "",
                "notice_type": "IG report",
            }
            n += 1
            if limit and n >= limit:
                return

    def discover(self, since: str, until: str | None = None,
                 limit: int | None = None) -> Iterator[dict]:
        n = 0
        api_pages: set[str] = set()
        for row in self._get():
            url = row.get("field_upload_document")
            if not url:
                continue                      # file lives on the OIG's own site
            posted = (row.get("field_publication_date") or "")[:10]
            if since and posted and posted < since:
                continue
            nid = str(row.get("nid") or "")
            api_pages.add("https://www.oversight.gov" + (row.get("path") or ""))
            yield {
                "source": "oversight",
                "notice_id": nid,
                "index": 0,
                "url": url,
                "landing_url": "https://www.oversight.gov" + (row.get("path") or ""),
                "title": (row.get("title") or "")[:300],
                "date": posted,
                "agency": (row.get("field_component_agency_name") or "")[:120],
                "office": (row.get("field_submitting_oig_name") or "")[:200],
                "notice_type": (row.get("field_type_of_product") or "")[:60],
                "report_number": (row.get("field_submitt_oig_report_number") or "")[:60],
            }
            n += 1
            if limit and n >= limit:
                return
        # The API is exhausted; the sitemap holds the rest.
        for rec in self.discover_sitemap(api_pages,
                                         None if limit is None else limit - n):
            yield rec

    def fetch(self, rec: dict) -> tuple[bytes, str]:
        req = urllib.request.Request(rec["url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
        name = rec["url"].rstrip("/").split("/")[-1].split("?")[0]
        return data, name
