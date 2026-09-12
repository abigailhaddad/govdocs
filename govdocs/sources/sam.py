"""sam.py — every attachment SAM.gov publishes, across all departments.

The Opportunities API is walked by notice type and date window. There is no
department filter to apply: SAM accepts a `deptname` parameter and ignores it,
returning the same total whether it is passed or not, so a filter here would
look like it worked and quietly do nothing. Everything is collected instead.

Volume is controlled by notice type and date window rather than by agency, since
that is what the API actually honours. August 2026 alone held 30,663 notices.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
from datetime import date, timedelta
from typing import Iterator

import requests

SEARCH_API = "https://api.sam.gov/prod/opportunities/v2/search"

# Every notice type SAM defines. p presolicitation, k combined synopsis,
# r sources sought, s special notice, o solicitation, g sale of surplus,
# i intent to bundle, a award notice, u justification.
ALL_PTYPES = ("p", "k", "r", "s", "o", "g", "i", "a", "u")

USER_AGENT = ("govdocs/0.1 (federal document archive; "
              "contact: abigail.haddad@gmail.com)")

WINDOW_DAYS = 15
SEEN_LOG = Path("data/seen.jsonl")
# Windows already paged to the end, so a later run does not spend calls
# re-reading them. See _done().
WINDOWS = Path("data/sam_windows.json")
# A window stays open for this long after it ends: SAM keeps accepting notices
# posted against a date that has passed, and closing a window the day it ends
# would miss them permanently.
SETTLE_DAYS = 10
PAGE = 1000
KEEP = re.compile(r"\.(pdf|docx?|xlsx?|pptx?)$", re.I)


def _mmddyyyy(d: date) -> str:
    return d.strftime("%m/%d/%Y")


class Sam:
    name = "sam"
    collection = "sam"

    _done_cache: set[str] | None = None

    def __init__(self, max_calls: int = 200, ptypes: tuple[str, ...] = ALL_PTYPES):
        if not os.environ.get("SAM_API_KEY"):
            raise SystemExit("SAM_API_KEY is not set. Get a free key at https://api.data.gov/signup/")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.max_calls = max_calls
        self.ptypes = ptypes
        self.calls = 0

    def _search(self, ptype: str, frm: date, to: date, offset: int) -> list[dict]:
        if self.calls >= self.max_calls:
            return []
        self.calls += 1
        try:
            r = self.session.get(SEARCH_API, timeout=120, params={
                "api_key": os.environ["SAM_API_KEY"],
                "postedFrom": _mmddyyyy(frm), "postedTo": _mmddyyyy(to),
                "ptype": ptype, "limit": PAGE, "offset": offset})
        except requests.RequestException:
            return []
        if r.status_code == 429:
            # Stop this source, do not end the run. Quota is shared and resets
            # at midnight UTC; the other sources have nothing to do with it, and
            # a SystemExit here took the whole collection step down with it.
            print("SAM 429 - daily quota exhausted; stopping this source "
                  "(resumes midnight UTC)")
            self.calls = self.max_calls
            return []
        if r.status_code != 200:
            return []
        return r.json().get("opportunitiesData") or []

    def _least_collected_first(self) -> list[str]:
        """Notice types we hold least of, first.

        Types were walked in declaration order -- every window of `p`, then
        every window of `k` -- and a run that stops on its call budget stops
        part way down that list. With 120 calls against a 360-call minimum it
        never got past the fourth of nine: Solicitation, Award Notice,
        Justification, Intent to Bundle and Sale of Surplus had zero rows in a
        16,714-row dataset, and every run reproduced that exactly.

        Ordering by what is already held means a type that has given nothing
        sorts first. Nothing is excluded; a long enough run still reaches all
        of them.
        """
        held: dict[str, int] = {}
        if SEEN_LOG.exists():
            for line in SEEN_LOG.read_text().splitlines():
                try:
                    r = json.loads(line)
                except Exception:
                    continue
                if r.get("doc_id") and r.get("ptype"):
                    held[r["ptype"]] = held.get(r["ptype"], 0) + 1
        return sorted(self.ptypes, key=lambda pt: (held.get(pt, 0), pt))

    def _done(self) -> set[str]:
        """(ptype, window) pairs already paged to the end.

        Without this a run re-reads every window it has ever finished. On
        2026-09-12 a pass spent its whole 5,000-call budget and the day's SAM
        quota to collect 15 documents, because interleaving the notice types
        made it start again at the beginning of each one. The documents were
        all already held; the calls were spent proving it.
        """
        if self._done_cache is None:
            try:
                self._done_cache = set(json.loads(WINDOWS.read_text()))
            except Exception:
                self._done_cache = set()
        return self._done_cache

    def _close(self, ptype: str, frm: date) -> None:
        done = self._done()
        done.add(f"{ptype}:{frm.isoformat()}")
        WINDOWS.parent.mkdir(parents=True, exist_ok=True)
        WINDOWS.write_text(json.dumps(sorted(done)))

    def _walk(self, ptype: str, start: date, end: date) -> Iterator[dict]:
        """Every attachment of one notice type, oldest window first."""
        settled = date.today() - timedelta(days=SETTLE_DAYS)
        frm = start
        while frm <= end:
            to = min(frm + timedelta(days=WINDOW_DAYS), end)
            if f"{ptype}:{frm.isoformat()}" in self._done():
                frm = to + timedelta(days=1)
                continue
            offset = 0
            complete = False
            while True:
                opps = self._search(ptype, frm, to, offset)
                if not opps:
                    break
                for o in opps:
                    notice = o.get("noticeId") or ""
                    path = o.get("fullParentPathName") or ""
                    for i, url in enumerate(o.get("resourceLinks") or []):
                        yield {
                            "source": "sam",
                            "notice_id": notice,
                            "index": i,
                            "url": url,
                            "landing_url": o.get("uiLink")
                            or f"https://sam.gov/opp/{notice}/view",
                            "title": (o.get("title") or "")[:300],
                            "date": (o.get("postedDate") or "")[:10],
                            "agency": path.split(".")[0][:120],
                            "office": path[:200],
                            "notice_type": o.get("type", "")[:60],
                            "ptype": ptype,
                        }
                if len(opps) < PAGE:
                    complete = True
                    break
                offset += PAGE
            # Only close a window that was read to the end AND has settled.
            # A window closed while notices are still being posted against it
            # loses them for good.
            if complete and to < settled:
                self._close(ptype, frm)
            frm = to + timedelta(days=1)

    def discover(self, since: str, until: str | None = None,
                 limit: int | None = None) -> Iterator[dict]:
        """All nine notice types at once, not one after another.

        Walking them in sequence means the budget decides which types exist in
        the dataset rather than which documents do, and it decides it the same
        way every run. Round-robin makes every type present in whatever the run
        manages to collect: a pass cut off after a tenth of the work holds a
        tenth of each type instead of all of two and none of seven.

        The order the walkers start in is still least-collected-first, so a
        short run puts its calls where the gaps are.
        """
        start = date.fromisoformat(since)
        end = date.fromisoformat(until) if until else date.today()
        walkers = [self._walk(pt, start, end) for pt in self._least_collected_first()]
        n = 0
        while walkers:
            for w in list(walkers):
                try:
                    rec = next(w)
                except StopIteration:
                    walkers.remove(w)
                    continue
                yield rec
                n += 1
                if limit and n >= limit:
                    return

    def fetch(self, rec: dict) -> tuple[bytes, str]:
        r = self.session.get(rec["url"], timeout=240, allow_redirects=True)
        r.raise_for_status()
        name = ""
        cd = r.headers.get("content-disposition", "")
        if "filename=" in cd:
            name = cd.split("filename=", 1)[1].strip().strip('"').strip("'")
        if not name:
            name = re.split(r"[?#]", rec["url"].rstrip("/").split("/")[-1])[0]
        return r.content, name
