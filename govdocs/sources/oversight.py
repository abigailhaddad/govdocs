"""oversight.py — every federal Inspector General, from one API.

oversight.gov is the government-wide clearing house for IG work, and its
api/v2/reports endpoint hands back the whole recent feed as JSON: report title,
submitting OIG, component agency, publication date, report number, product type
and a direct document URL. One call covered 47 distinct OIGs -- FDIC, Treasury,
Labor, SBA, HHS, TIGTA, the pandemic-recovery special IG and the rest.

The endpoint ignores pagination. page, offset and items_per_page all return the
same 1,063 rows, so this is a rolling window of recent reports rather than an
archive to walk. Coverage grows by running it regularly, not by asking for more.

Of that window, 841 rows carried a document URL; the remainder are reports whose
file lives on the originating OIG's own site.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Iterator

API = "https://www.oversight.gov/api/v2/reports"
USER_AGENT = ("govdocs/0.1 (federal document archive; "
              "contact: abigail.haddad@gmail.com)")


class Oversight:
    name = "oversight"
    collection = "foia"

    def __init__(self, max_calls: int = 4):
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

    def discover(self, since: str, until: str | None = None,
                 limit: int | None = None) -> Iterator[dict]:
        n = 0
        for row in self._get():
            url = row.get("field_upload_document")
            if not url:
                continue                      # file lives on the OIG's own site
            posted = (row.get("field_publication_date") or "")[:10]
            if since and posted and posted < since:
                continue
            nid = str(row.get("nid") or "")
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

    def fetch(self, rec: dict) -> tuple[bytes, str]:
        req = urllib.request.Request(rec["url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as resp:
            data = resp.read()
        name = rec["url"].rstrip("/").split("/")[-1].split("?")[0]
        return data, name
