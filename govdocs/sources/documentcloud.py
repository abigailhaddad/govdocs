"""documentcloud.py — FOIA releases as the people who requested them uploaded them.

Reading rooms hold what agencies are obliged to post: the four categories in
5 U.S.C. 552(a)(2), plus records already requested three or more times. A
one-off release to a journalist or a researcher goes to that person and nowhere
else, which is most of what FOIA actually produces.

DocumentCloud is where a lot of it ends up. 455,300 public documents match
"foia" alone, uploaded by the newsrooms that received them, with no obligation
filtering what appears. Its search API needs no credentials for public
documents.

Queries name the kinds of release worth having rather than sweeping everything:
the corpus is mostly newsroom working material, and an untargeted crawl would
bring back court filings and press clippings alongside the releases.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from typing import Iterator

SEARCH = "https://api.www.documentcloud.org/api/documents/search/"
ASSET = "https://s3.documentcloud.org/documents/{id}/{slug}.pdf"
USER_AGENT = ("govdocs/0.1 (federal document archive; "
              "contact: abigail.haddad@gmail.com)")

# Ordering by date destroys relevance ranking -- the API then returns the
# newest public uploads regardless of the query, which is town council agendas.
# These run on relevance, and are phrased to match how agencies title a release
# rather than to match any mention of FOIA: "freedom of information act" alone
# matches 396,713 documents, most of them citing the statute rather than being
# a product of it.
QUERIES = (
    '"final response" AND foia',        # 20,218: how agencies title a release
    'title:foia AND redacted',
    '"responsive records" AND released',
    '"FOIA request" AND "enclosed"',
    'title:"foia release"',
    # FOIAonline was the shared request portal for around twenty agencies until
    # it was shut down in 2023. MuckRock and POGO captured roughly 34,000
    # documents from it -- 110GB, mostly EPA, NLRB, GSA and DLA -- and put them
    # here rather than anywhere of their own, so this is the only route to
    # releases from a system that no longer exists.
    'foiaonline',
)

PER_PAGE = 100


class DocumentCloud:
    name = "documentcloud"
    collection = "foia"

    def __init__(self, max_calls: int = 200):
        self.max_calls = max_calls
        self.calls = 0

    def _get(self, url: str) -> dict:
        self.calls += 1
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read())

    def discover(self, since: str, until: str | None = None,
                 limit: int | None = None) -> Iterator[dict]:
        n = 0
        seen: set[int] = set()
        for q in QUERIES:
            url = f"{SEARCH}?q={urllib.parse.quote(q)}&per_page={PER_PAGE}"
            while url and self.calls < self.max_calls:
                try:
                    d = self._get(url)
                except Exception:
                    break
                for r in d.get("results") or []:
                    if r.get("access") != "public" or r.get("status") != "success":
                        continue
                    doc_id = r.get("id")
                    if not doc_id or doc_id in seen:
                        continue
                    seen.add(doc_id)
                    created = (r.get("created_at") or "")[:10]
                    if since and created and created < since:
                        continue
                    slug = r.get("slug") or str(doc_id)
                    yield {
                        "source": "documentcloud",
                        "notice_id": str(doc_id),
                        "index": 0,
                        "url": ASSET.format(id=doc_id, slug=slug),
                        "landing_url": r.get("canonical_url") or "",
                        "title": (r.get("title") or slug)[:300],
                        "date": created,
                        # Who uploaded it, which is the closest thing to
                        # provenance DocumentCloud exposes without another call.
                        "agency": str((r.get("organization") or ""))[:120],
                        "office": "",
                        "notice_type": "FOIA release (DocumentCloud)",
                        "query": q,
                    }
                    n += 1
                    if limit and n >= limit:
                        return
                url = d.get("next")

    def fetch(self, rec: dict) -> tuple[bytes, str]:
        req = urllib.request.Request(rec["url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
        return data, rec["url"].rstrip("/").split("/")[-1]
