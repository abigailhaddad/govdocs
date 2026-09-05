"""profiles.py — what we know about how individual agencies lay out their FOIA pages.

The generic crawler in foia_rooms.py finds whatever is linked from a reading
room's front page. That is the floor, not the ceiling: most agencies paginate,
and the front page is one slice of a much larger library. DOJ's Office of Legal
Counsel publishes 1,453 opinions across 146 pages and links 11 of them from the
page the directory points at.

A profile says three things about a host: how to page through a listing, how to
recognise a document link, and how to pull a title out of the row around it.
Agencies get added here one at a time as their layout is worked out; anything
without a profile still gets the generic treatment.

Keep these keyed by HOST, not by agency. Departments share platforms -- every
justice.gov reading room is the same Drupal theme -- so one profile covers all
23 DOJ components, and knowing that is most of the value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class Profile:
    host: str
    # Appended to a listing URL to request page N (0-indexed unless noted).
    page_param: str | None = "?page={n}"
    max_pages: int = 40
    # Captures (href, title). Falls back to bare document links when unset.
    row_re: re.Pattern | None = None
    # Extra link shape to treat as a document beyond the usual extensions.
    doc_re: re.Pattern | None = None
    per_second: float = 0.5
    # "browser" drives a real, HEADED browser for LISTING pages. Headless is
    # not enough: headless chromium gets "Access Denied" from justice.gov and
    # the same launch headed returns the page, so the wall is reading the
    # browser rather than the address or the rate. Some agencies serve
    # their front page to anything and 403 every paginated request, browser
    # User-Agent, Accept, Accept-Language and Referer included. A real browser
    # clears it and costs nothing, so reach for this before any paid unblocker.
    # Document URLs are still fetched over plain HTTP -- only the listings are
    # gated -- which keeps the browser out of the download path entirely.
    fetch_mode: str = "direct"
    notes: str = ""


PROFILES: dict[str, Profile] = {
    # Every justice.gov reading room runs the same theme. Files are served from
    # /<component>/media/<id>/dl rather than as .pdf links, which is why a naive
    # PDF scrape of a DOJ page finds nothing at all.
    "www.justice.gov": Profile(
        host="www.justice.gov",
        page_param="?page={n}",
        max_pages=150,
        row_re=re.compile(
            r'<a href="(/[^"]*?/media/\d+/dl)"[^>]*>\s*'
            r'(?:<span[^>]*>)?\s*([^<]{4,300}?)\s*(?:</span>)?\s*</a>', re.I | re.S),
        doc_re=re.compile(r"/media/\d+/dl"),
        fetch_mode="browser",
        notes=("OLC alone: 1,453 opinions over 146 pages, 11 per page. The front "
               "page serves fine but ?page=N returns 403 to any direct request, "
               "browser headers included. A headed browser clears it; headless "
               "gets Access Denied. The "
               "/media/<id>/dl document URLs are NOT gated and fetch over plain "
               "HTTP at full speed."),
    ),
    "www.uscis.gov": Profile(
        host="www.uscis.gov",
        page_param="?page={n}",
        max_pages=40,
    ),
    "www.cbp.gov": Profile(
        host="www.cbp.gov",
        page_param="?page={n}",
        max_pages=40,
    ),
    "www.ice.gov": Profile(
        host="www.ice.gov",
        page_param="?page={n}",
        max_pages=180,
        notes="Detention oversight inspections; 180 pages of ~25 documents.",
    ),
    "www.oig.dhs.gov": Profile(
        host="www.oig.dhs.gov",
        page_param="?page={n}",
        max_pages=60,
    ),
    # 403 to any direct request, 200 to a headed browser. The Vault is a
    # browse tree rather than a paged listing, so the generic same-host crawl
    # does the walking and this only supplies the way in.
    "vault.fbi.gov": Profile(
        host="vault.fbi.gov",
        page_param=None,
        fetch_mode="browser",
        notes="Direct fetches get 403; headed browser gets 200.",
    ),
    "www.hhs.gov": Profile(
        host="www.hhs.gov",
        page_param="?page={n}",
        max_pages=40,
        fetch_mode="browser",
        notes="403 direct, 200 headed. 10 documents linked from the front page.",
    ),
    "oig.justice.gov": Profile(
        host="oig.justice.gov",
        page_param="?page={n}",
        max_pages=150,
    ),
}


def for_url(url: str) -> Profile | None:
    from urllib.parse import urlsplit
    return PROFILES.get(urlsplit(url).netloc)
