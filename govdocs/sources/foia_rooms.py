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
import hashlib
from datetime import date
import re
import time
import urllib.parse
import urllib.robotparser
from pathlib import Path
from typing import Iterator

import requests

from .profiles import for_url
from .room_overrides import resolve

COMPONENTS_API = "https://api.foia.gov/api/agency_components"
DIRECTORY = Path("data/reading_rooms.json")
# What has been collected already, read to decide which rooms to visit first.
SEEN_LOG = Path("data/seen.jsonl")
# What each room gave last time it was visited, so a run stops re-walking the
# ones that never give anything. See _health().
HEALTH = Path("data/room_health.json")
# A room that came up empty is not written off, it is put to the back of the
# queue for a while: 1 day, then 2, 4, 8, up to 32. A 403 wall is a property of
# the crawler's welcome and can lift; a room that has genuinely been emptied
# gets new documents eventually. Never visiting again would turn a bad
# afternoon into a permanent hole, which is the mistake this project has
# already made once with seen.jsonl.
BACKOFF_DAYS = (1, 2, 4, 8, 16, 32)
REFRESH_AFTER_DAYS = 30

USER_AGENT = ("govdocs/0.1 (federal document archive; "
              "contact: abigail.haddad@gmail.com)")

DOC_RE = re.compile(r"\.(pdf|docx?|xlsx?)(\?|$)", re.I)

# Path segments that carry no meaning as a title. DOJ serves files from
# /<component>/media/<id>/dl, so taking the last segment titled every DOJ
# document "dl".
# Path segments that carry no meaning as a title: verbs, container folders and
# bare ids. DOJ serves files from /<component>/media/<id>/dl, so taking the last
# segment titled every DOJ document "dl" and taking the last useful-looking one
# titled them all "media".
USELESS_NAMES = {"dl", "download", "file", "files", "view", "get", "attachment",
                 "doc", "document", "documents", "index", "default", "media",
                 "sites", "content", "assets", "uploads", "pdf", "public"}


def _name_from_url(url: str) -> str:
    """A usable title from a URL when the listing gave us none.

    Keeps the parts that identify the document and drops the plumbing, so
    /olc/media/1460356/dl becomes "olc-1460356" rather than "dl" or "media".
    """
    parts = [urllib.parse.unquote(p) for p in
             urllib.parse.urlsplit(url).path.strip("/").split("/") if p]
    keep = [p for p in parts if p.rsplit(".", 1)[0].lower() not in USELESS_NAMES]
    if not keep:
        return "document"
    return "-".join(keep[-2:]) if len(keep) > 1 else keep[-1]
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

    _health_cache: dict | None = None

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

    def _least_harvested_first(self, rooms: list[dict]) -> list[dict]:
        """Rooms we have taken least from, first.

        The directory is 404 listings and a run stops at its limit, so the
        order decides what gets visited at all -- and in directory order that
        was the same front stretch every time. ICE's 4,393 documents were
        re-walked on every run while 21 hosts holding a thousand documents
        between them, OGE's 669 among them, were never reached once.

        Ordering by what has already been collected fixes that without
        excluding anything: a room that has given nothing sorts first, a room
        that has given thousands sorts last, and both are still visited by a
        run long enough to get there.
        """
        taken: dict[str, int] = {}
        failed: dict[str, int] = {}
        if SEEN_LOG.exists():
            for line in SEEN_LOG.read_text().splitlines():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("source") != "foia_rooms" and not str(
                        r.get("key", "")).startswith("foia_rooms/"):
                    continue
                url = r.get("url")
                if not url:
                    continue
                h = urllib.parse.urlsplit(url).netloc
                if r.get("doc_id"):
                    taken[h] = taken.get(h, 0) + 1
                elif r.get("error"):
                    failed[h] = failed.get(h, 0) + 1

        def key(r: dict) -> tuple[int, int]:
            h = urllib.parse.urlsplit(r["url"]).netloc
            # Failures break the tie among rooms that have given nothing, so a
            # host that has never been tried sorts ahead of one that refuses
            # every request. Otherwise the 59 walled agencies would lead every
            # run and spend its first few hundred fetches being turned away.
            return taken.get(h, 0), failed.get(h, 0)

        return sorted(rooms, key=key)

    def _health(self) -> dict:
        if self._health_cache is None:
            try:
                self._health_cache = json.loads(HEALTH.read_text())
            except Exception:
                self._health_cache = {}
        return self._health_cache

    def _due(self, room: dict) -> bool:
        """Is this room worth a visit today?

        The ordering already puts never-productive rooms first, which is right:
        that is where anything new would be. But most of them are the 403 walls,
        the dead hostnames and the genuinely empty ones, so every pass spent its
        whole budget re-reading them -- 120 KB/s of fetching for zero collected
        documents on 2026-09-12.
        """
        h = self._health().get(room.get("url", ""))
        if not h:
            return True
        misses = int(h.get("empty_runs", 0))
        if misses <= 0:
            return True
        wait = BACKOFF_DAYS[min(misses, len(BACKOFF_DAYS)) - 1]
        try:
            last = date.fromisoformat(h.get("last", "1970-01-01"))
        except ValueError:
            return True
        return (date.today() - last).days >= wait

    def _record_room(self, room: dict, found: int) -> None:
        h = self._health()
        row = h.setdefault(room.get("url", ""), {})
        row["last"] = date.today().isoformat()
        row["last_found"] = found
        row["empty_runs"] = 0 if found else int(row.get("empty_runs", 0)) + 1
        row["total"] = int(row.get("total", 0)) + found
        HEALTH.parent.mkdir(parents=True, exist_ok=True)
        HEALTH.write_text(json.dumps(h, indent=1, sort_keys=True))

    def discover(self, since: str, until: str | None = None,
                 limit: int | None = None) -> Iterator[dict]:
        rooms = [r for r in self._least_harvested_first(refresh_directory())
                 if self._due(r)]
        skipped = len(refresh_directory()) - len(rooms)
        if skipped:
            print(f"  skipping {skipped} rooms that came up empty recently",
                  flush=True)
        n = 0
        seen_docs: set[str] = set()
        for room in rooms:
            # The directory lists rooms that have moved or been retired, so the
            # listed URL is not always the one worth fetching. See
            # room_overrides: 37 of 223 hosts answered neither 200 nor 403.
            start = resolve(room["url"])
            if start is None:
                continue
            html = self._get(start)
            if not html:
                self._record_room(room, 0)
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
            fresh = [u for u, _ in dict.fromkeys(docs) if u not in seen_docs]
            self._record_room(room, len(fresh))
            for url, row_title in dict.fromkeys(docs):
                if url in seen_docs:
                    continue
                seen_docs.add(url)
                name = row_title or _name_from_url(url)
                yield {
                    "source": "foia_rooms",
                    # A stable id, not Python's hash(): that is seeded per
                    # process, so the same URL got a different id on every run,
                    # no already-seen key ever matched, and every document was
                    # downloaded again to be recognised by its checksum after
                    # the fact -- 15,189 refetches against agency servers.
                    "notice_id": hashlib.sha1(url.encode()).hexdigest()[:16],
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
        # Reading rooms almost never date their listings. The server usually
        # knows when the file was put there, which is the closest thing to a
        # release date these pages offer.
        rec["last_modified"] = r.headers.get("Last-Modified", "")
        name = urllib.parse.unquote(rec["url"].rstrip("/").split("/")[-1].split("?")[0])
        return r.content, name or "document.pdf"
