"""foia_rooms.py — documents from every federal FOIA reading room.

api.foia.gov publishes the government's own directory of FOIA offices, and each
component record carries the URL of its reading room. That is 615 components and
311 reading rooms across 223 hosts -- DOJ alone lists 23 -- which is the whole
federal FOIA estate without writing a scraper per agency.

Their layouts have nothing in common, so this does not try to parse them. It
fetches a reading room, takes every document link on the page, and follows
same-host links that look like further listing pages one level deep. Crude, but
it works everywhere and degrades to "found nothing here" rather than breaking.

robots.txt is honoured per host, fetched once and cached, and every host gets its
own rate limiter: 223 agencies, none of whom asked to be crawled.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path
from typing import Iterator

import requests

from .profiles import for_url

COMPONENTS_API = "https://api.foia.gov/api/agency_components"
DIRECTORY = Path("data/reading_rooms.json")
REFRESH_AFTER_DAYS = 30

USER_AGENT = ("govdocs/0.1 (federal document archive; "
              "contact: abigail.haddad@gmail.com)")

DOC_RE = re.compile(r"\.(pdf|docx?|xlsx?)(\?|$)", re.I)
# Pages that look like more of the same listing rather than site furniture.
LISTING_RE = re.compile(
    r"(foia|reading[-_]?room|library|records|releases?|logs?|disclosure"
    r"|frequently[-_]requested|page=\d+|\?page)", re.I)

PER_HOST_DELAY = 2.0
MAX_LISTING_PAGES = 8


def refresh_directory(api_key: str | None = None, force: bool = False) -> list[dict]:
    """The government's own list of FOIA offices and their reading rooms."""
    if DIRECTORY.exists() and not force:
        age = (time.time() - DIRECTORY.stat().st_mtime) / 86400
        if age < REFRESH_AFTER_DAYS:
            return json.loads(DIRECTORY.read_text())

    key = api_key or os.environ.get("DATAGOV_API_KEY") or "DEMO_KEY"
    out, url, pages = [], f"{COMPONENTS_API}?api_key={key}", 0
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    while url and pages < 60:
        r = session.get(url, timeout=90)
        if r.status_code != 200:
            break
        d = r.json()
        for row in d.get("data") or []:
            a = row.get("attributes") or {}
            for rr in a.get("reading_rooms") or []:
                uri = rr.get("uri") if isinstance(rr, dict) else None
                if uri:
                    out.append({"url": uri,
                                "abbreviation": a.get("abbreviation") or "",
                                "title": rr.get("title") or ""})
        nxt = (d.get("links") or {}).get("next")
        url = nxt.get("href") if isinstance(nxt, dict) else None
        if url and "api_key=" not in url:
            url += ("&" if "?" in url else "?") + "api_key=" + key
        pages += 1
        time.sleep(0.4)
    if out:
        DIRECTORY.parent.mkdir(parents=True, exist_ok=True)
        DIRECTORY.write_text(json.dumps(out, indent=0))
    return out


class FoiaRooms:
    name = "foia_rooms"
    collection = "foia"

    def __init__(self, max_calls: int = 400):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.max_calls = max_calls
        self.calls = 0
        self._robots: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._last_hit: dict[str, float] = {}
        self._pw = None            # playwright driver, started only if needed
        self._browser = None
        self._page = None
        # Hosts that answered a plain request with a refusal. Roughly a third of
        # federal FOIA hosts do -- 33 of the first 112 probed, including ATF,
        # DEA, FAA, FCC, FERC, DoD IG and most .mil sites. Writing a profile for
        # each would be seventy entries of the same fact, so the refusal is
        # simply noticed and the host switched to a browser from then on.
        self._needs_browser: set[str] = set()

    def _allowed(self, url: str) -> bool:
        """Does this host's robots.txt permit the URL?

        robots.txt is fetched with our own session rather than by
        RobotFileParser.read(), which treats a 403 as disallow-all. Several
        agencies refuse any non-browser request, robots.txt included, so trusting
        that turned "this host blocks our fetcher" into "this host forbids
        crawling" -- and we blocked ourselves from FBI Vault and HHS, both of
        whose published robots.txt allow everything we wanted.

        A 403 or a network failure is not a directive. A 404 means no rules at
        all. Only a file we actually read is obeyed.
        """
        host = urllib.parse.urlsplit(url).netloc
        if host not in self._robots:
            rp = None
            try:
                r = self.session.get(
                    f"{urllib.parse.urlsplit(url).scheme}://{host}/robots.txt", timeout=30)
                if r.status_code == 200 and r.text.strip():
                    rp = urllib.robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except requests.RequestException:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        if rp is None:
            return True
        try:
            return rp.can_fetch(USER_AGENT, url)
        except Exception:
            return True

    def _wait(self, url: str) -> None:
        host = urllib.parse.urlsplit(url).netloc
        gap = PER_HOST_DELAY - (time.monotonic() - self._last_hit.get(host, 0))
        if gap > 0:
            time.sleep(gap)
        self._last_hit[host] = time.monotonic()

    def _browser_get(self, url: str) -> str | None:
        """Fetch a listing page with a real browser.

        Started lazily and reused: launching one per page would be slower than
        the sites are. Only listing pages come through here; documents download
        over plain HTTP.
        """
        if self._page is None:
            try:
                from playwright.sync_api import sync_playwright
            except ImportError:
                return None
            self._pw = sync_playwright().start()
            # headless=False matters. Headless chromium gets "Access Denied"
            # from justice.gov; the same launch headed returns the page. The
            # wall reads the browser, not the IP or the rate.
            self._browser = self._pw.chromium.launch(headless=False)
            self._page = self._browser.new_page()
        try:
            self._page.goto(url, timeout=60000, wait_until="domcontentloaded")
            return self._page.content()
        except Exception:
            return None

    def close(self) -> None:
        for obj, meth in ((self._browser, "close"), (self._pw, "stop")):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:
                pass
        self._pw = self._browser = self._page = None

    def _get(self, url: str) -> str | None:
        if self.calls >= self.max_calls or not self._allowed(url):
            return None
        prof = for_url(url)
        self._wait(url)
        self.calls += 1
        host = urllib.parse.urlsplit(url).netloc
        if (prof and prof.fetch_mode == "browser") or host in self._needs_browser:
            return self._browser_get(url)
        try:
            r = self.session.get(url, timeout=60)
        except requests.RequestException:
            return None
        if r.status_code in (401, 403, 503):
            # Not a refusal to be crawled, just a refusal to be crawled by this.
            # A browser gets through, so remember the host and use one.
            self._needs_browser.add(host)
            return self._browser_get(url)
        if r.status_code != 200:
            return None
        if "html" not in r.headers.get("content-type", "") and "<html" not in r.text[:400].lower():
            return None
        return r.text

    def _links(self, html: str, base: str) -> tuple[list[tuple[str, str]], list[str]]:
        """Document links (with a title where the profile can find one), and
        further listing pages."""
        if not html:
            return [], []
        prof = for_url(base)
        docs: list[tuple[str, str]] = []
        listings: list[str] = []
        host = urllib.parse.urlsplit(base).netloc

        if prof and prof.row_re is not None:
            for m in prof.row_re.finditer(html):
                href = urllib.parse.urljoin(base, m.group(1))
                title = re.sub(r"\s+", " ", m.group(2)).strip()
                docs.append((href, title[:300]))

        for m in re.finditer(r'href="([^"#]+)"', html, re.I):
            href = urllib.parse.urljoin(base, m.group(1))
            if urllib.parse.urlsplit(href).netloc != host:
                continue
            if DOC_RE.search(href) or (prof and prof.doc_re and prof.doc_re.search(href)):
                docs.append((href, ""))
            elif LISTING_RE.search(href):
                listings.append(href)

        seen, out = set(), []
        for href, title in docs:
            if href in seen:
                continue
            seen.add(href)
            out.append((href, title))
        return out, list(dict.fromkeys(listings))

    def discover(self, since: str, until: str | None = None,
                 limit: int | None = None) -> Iterator[dict]:
        rooms = refresh_directory()
        n = 0
        seen_docs: set[str] = set()
        for room in rooms:
            start = room["url"]
            html = self._get(start)
            if not html:
                continue
            docs, listings = self._links(html, start)

            prof = for_url(start)
            if prof and prof.page_param:
                # Walk the agency's own pagination rather than guessing from
                # links: the front page is a small slice of the library.
                for page_no in range(1, prof.max_pages):
                    nxt = start + prof.page_param.format(n=page_no)
                    sub = self._get(nxt)
                    if not sub:
                        break
                    more, _ = self._links(sub, nxt)
                    if not more:
                        break
                    docs.extend(more)
            else:
                for page in listings[:MAX_LISTING_PAGES]:
                    sub = self._get(page)
                    if sub:
                        more, _ = self._links(sub, page)
                        docs.extend(more)
            for url, row_title in dict.fromkeys(docs):
                if url in seen_docs:
                    continue
                seen_docs.add(url)
                name = row_title or urllib.parse.unquote(
                    url.rstrip("/").split("/")[-1].split("?")[0])
                yield {
                    "source": "foia_rooms",
                    "notice_id": str(abs(hash(url)) % (10 ** 12)),
                    "index": 0,
                    "url": url,
                    "landing_url": start,
                    "title": name[:300],
                    "date": "",
                    "agency": room.get("abbreviation", "")[:120],
                    "office": room.get("title", "")[:200],
                    "notice_type": "FOIA reading room",
                }
                n += 1
                if limit and n >= limit:
                    return

    def fetch(self, rec: dict) -> tuple[bytes, str]:
        self._wait(rec["url"])
        r = self.session.get(rec["url"], timeout=240, allow_redirects=True)
        r.raise_for_status()
        name = urllib.parse.unquote(rec["url"].rstrip("/").split("/")[-1].split("?")[0])
        return r.content, name or "document.pdf"
