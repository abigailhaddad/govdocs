"""documentcloud.py — FOIA releases as the people who requested them uploaded them.

Reading rooms hold what agencies are obliged to post: the four categories in
5 U.S.C. 552(a)(2), plus records already requested three or more times. A
one-off release to a journalist goes to that person and nowhere else, which is
most of what FOIA produces. It shows in what the reading rooms gave us: of 2,126
documents, 242 were FOIA logs and about 33 were actual released records.

DocumentCloud is where much of the rest ends up -- 455,300 public documents
match "foia" -- including the roughly 34,000 rescued from FOIAonline before that
system was shut down in 2023, which exist nowhere else.

On whether to collect it. The test used here is whether a credential was needed.
This endpoint serves public documents to anyone with no key, no account and no
agreement entered, and the documents are US government works carrying no
copyright. MuckRock's own API returns 401 without an account, and signing up
means accepting terms -- so that one is not used, and should not be worked
around. Public and unauthenticated is a different thing from credentialed.

Two API details. `order=created_at` keeps the query filter and sorts what it
matched -- checked: `"final response" AND foia` returns the same 20,219 either
way. What it does not survive is an untargeted query: ordering a bare `foia` by
date returns the newest uploads that mention the statute, which is town council
agendas. So the ordering is safe here precisely because the queries below are
narrow, and would not be safe without them.

And "freedom of information act" matches 396,713 documents, most citing the
statute rather than resulting from it, so the queries match how agencies title a
release instead.

Ordering newest-first is the point: it front-loads recent releases without
excluding anything, so a bounded run spends its budget on what was released
lately and older material still arrives if the run is long enough.
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

# Phrased to match how agencies title a release rather than any mention of FOIA.
# The narrowness is what makes date-ordering safe: "freedom of information act" alone
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


def _org_name(org: object) -> str:
    """The uploading organisation's name. Unexpanded it is a bare id."""
    if isinstance(org, dict):
        return str(org.get("name") or org.get("slug") or "")
    return ""


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
            # order=created_at sorts this query's matches newest-first; it
            # does not widen them. expand=organization turns the uploader from
            # a bare id into a name.
            url = (f"{SEARCH}?q={urllib.parse.quote(q)}&per_page={PER_PAGE}"
                   f"&order=created_at&expand=organization")
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
                    # Deliberately not filtered on `since`. Results arrive
                    # newest-first, so a bounded run already spends itself on
                    # recent releases; dropping older ones would lose documents
                    # that are wanted, just wanted less.
                    slug = r.get("slug") or str(doc_id)
                    yield {
                        "source": "documentcloud",
                        "notice_id": str(doc_id),
                        "index": 0,
                        "url": ASSET.format(id=doc_id, slug=slug),
                        "landing_url": r.get("canonical_url") or "",
                        "title": (r.get("title") or slug)[:300],
                        "date": created,
                        # Left blank on purpose. The only party DocumentCloud
                        # names is whoever uploaded the file, and a newsroom is
                        # not the agency that released it -- putting "MuckRock
                        # Staff" in the agency column would corrupt the one
                        # field the whole archive is filtered on.
                        "agency": "",
                        "office": _org_name(r.get("organization"))[:200],
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
