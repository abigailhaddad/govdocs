"""sam.py — every attachment SAM.gov publishes, across all departments.

The Opportunities API is walked by notice type and date window. There is no
department filter to apply: SAM accepts a `deptname` parameter and ignores it,
returning the same total whether it is passed or not, so a filter here would
look like it worked and quietly do nothing. Everything is collected instead.

Volume is controlled by notice type and date window rather than by agency, since
that is what the API actually honours. August 2026 alone held 30,663 notices.
"""

from __future__ import annotations

import os
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
PAGE = 1000
KEEP = re.compile(r"\.(pdf|docx?|xlsx?|pptx?)$", re.I)


def _mmddyyyy(d: date) -> str:
    return d.strftime("%m/%d/%Y")


class Sam:
    name = "sam"
    collection = "sam"

    def __init__(self, max_calls: int = 200, ptypes: tuple[str, ...] = ALL_PTYPES):
        if not os.environ.get("SAM_API_KEY"):
            raise SystemExit("SAM_API_KEY is not set; it lives in pull_usaspending/.env")
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
            raise SystemExit("SAM 429 - daily quota exhausted; resumes midnight UTC")
        if r.status_code != 200:
            return []
        return r.json().get("opportunitiesData") or []

    def discover(self, since: str, until: str | None = None,
                 limit: int | None = None) -> Iterator[dict]:
        start = date.fromisoformat(since)
        end = date.fromisoformat(until) if until else date.today()
        n = 0
        for ptype in self.ptypes:
            frm = start
            while frm <= end:
                to = min(frm + timedelta(days=WINDOW_DAYS), end)
                offset = 0
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
                            n += 1
                            if limit and n >= limit:
                                return
                    if len(opps) < PAGE:
                        break
                    offset += PAGE
                frm = to + timedelta(days=1)

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
