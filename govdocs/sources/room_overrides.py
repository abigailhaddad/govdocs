"""room_overrides.py -- corrections to the government's own FOIA directory.

api.foia.gov lists reading rooms that no longer exist. Probing all 223 hosts in
the directory: 126 answered, 59 refused a plain request (the known wall -- ATF,
DEA, the DOT family, most .mil), and 37 were neither -- 404s, dead hostnames,
expired certificates. That last group is rot rather than blocking, and about
half of it is repairable: the agency still publishes a FOIA library, at an
address the directory never learned.

Each replacement below was checked to return 200 and to carry either document
links or a link to a further reading-room page. Counts are from the day they
were verified and will drift; they are here to show the entry was not guessed.

Entries the directory gets right are not listed. This is only the diff.
"""

from __future__ import annotations

# stale directory URL -> the page that actually holds the documents
OVERRIDES: dict[str, str] = {
    # BPA: 0 document links, 1 reading-room sublinks
    'https://www.bpa.gov/news/FOIA/library/Pages/default.aspx':
        'https://www.bpa.gov/about/who-we-are/freedom-of-information-act',
    # CBFO: 1 document links, 0 reading-room sublinks
    'http://www.wipp.energy.gov/library/foia/FOIA_Reading_Room.htm':
        'https://wipp.energy.gov/foia-current-contracts.asp',
    # COPS: 9 document links, 0 reading-room sublinks
    'https://cops.usdoj.gov/Default.asp?Item=40':
        'https://cops.usdoj.gov/foia',
    # CPPBSD: 26 document links, 3 reading-room sublinks
    'http://www.abilityone.gov/laws,_regulations_and_policy/foia_reading_room.html':
        'https://www.abilityone.gov/laws,_regulations_and_policy/foia_reading_room.html',
    # FLETC: 18 document links, 1 reading-room sublinks
    'https://www.fletc.gov/archive/freedom-information-library':
        'https://www.fletc.gov/freedom-information-library',
    # FMSHRC: 25 document links, 3 reading-room sublinks
    'http://www.fmshrc.gov/foia/e-reading-room':
        'https://www.fmshrc.gov/content/foia-library',
    # Fiscal Service: 1 document links, 1 reading-room sublinks
    'https://www.fiscal.treasury.gov/foia-readingroom.html':
        'https://fiscal.treasury.gov/about-us/foia',
    # GCERC: 2 document links, 0 reading-room sublinks
    'https://www.restorethegulf.gov/resources/council-documents-foia-library':
        'https://www.restorethegulf.gov/reports/annual-foia-reports/',
    # GSA-Main: 0 document links, 1 reading-room sublinks
    'https://www.gsa.gov/portal/content/305477':
        'https://www.gsa.gov/reference/freedom-of-information-act-foia',
    # HQ: 0 document links, 3 reading-room sublinks
    'https://energy.gov/management/office-management/operational-management/freedom-information-act/reading-room':
        'https://www.energy.gov/gc/foia-reading-room',
    # OSC: 0 document links, 1 reading-room sublinks
    'https://osc.gov/Pages/FOIA-Resources.aspx':
        'https://osc.gov/about/foia/overview',
    # OSM: 4 document links, 1 reading-room sublinks
    'https://www.osmre.gov/lrg/foia.shtm':
        'https://www.osmre.gov/laws-and-regulations/foia',
    # USIBWC: 36 document links, 0 reading-room sublinks
    'https://www.ibwc.gov/Organization/FOIA_RR.html':
        'https://www.ibwc.gov/foia/',
    # USSOCOM: 6 document links, 1 reading-room sublinks
    'http://www.socom.mil/FOIA/Pages/ReadingRoom.aspx':
        'https://www.socom.mil/foia',
    # USTRANSCOM: 3 document links, 0 reading-room sublinks
    'https://www.ustranscom.mil/foia/index.cfm?thisview=readroom':
        'https://www.business.ustranscom.mil/foia/',
}


# Hosts that do not resolve, do not answer, or serve an expired certificate,
# and for which no replacement was found. Several are agencies that no longer
# exist -- OPIC became the DFC, the NSCAI dissolved, FOIAonline was retired --
# so there is nothing to point at. Listed rather than deleted so a later run
# does not rediscover them as news.
KNOWN_DEAD: frozenset[str] = frozenset({
    'foiaonline.gov',  # CBP: ConnectionError
    'foia.cdc.gov',  # CDC: ConnectionError
    'www.rmda.army.mil',  # DA: ConnectTimeout
    'www.osec.doc.gov',  # DOC: ConnectionError
    'www.secnav.navy.mil',  # DON: ConnectTimeout
    'www.fsa.usda.gov',  # FSA: ReadTimeout
    'www.id.doe.gov',  # ID: ConnectionError
    'osec.doc.gov',  # NIST: ConnectionError
    'www.nmb.gov',  # NMB: 404
    'www.nscai.gov',  # NSCAI: ConnectionError
    'oig.state.gov',  # OIG: ConnectionError
    'www.onhir.gov',  # ONHIR: ConnectionError
    'www.opic.gov',  # OPIC: ConnectionError
    'www.sigar.mil',  # SIGAR: ConnectionError
    'www.swpa.gov',  # SWPA: ConnectionError
    'www.wapa.gov',  # WAPA: SSLError
})


def resolve(url: str) -> str | None:
    """The URL worth fetching for this listing, or None to skip it."""
    if url in OVERRIDES:
        return OVERRIDES[url]
    host = url.split("/")[2] if "//" in url else ""
    if host in KNOWN_DEAD:
        return None
    return url

