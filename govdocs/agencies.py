"""agencies.py — one spelling per agency.

Sources name the same body differently. SAM says "DEPT OF DEFENSE", the FOIA
directory says "DoD", oversight.gov says "Department of War", a reading room
says "SOL" and means Interior's Solicitor. Anyone filtering the dataset by
agency needs one spelling, so the collected value is kept as `agency_raw` and a
canonical name is derived alongside it.

The mapping is deliberately explicit rather than clever. A wrong guess here is
invisible -- it silently files a document under the wrong department -- so
anything unrecognised keeps its original value rather than being forced into the
nearest match.
"""

from __future__ import annotations

import re

# Abbreviations and variants -> canonical department or independent agency.
CANONICAL: dict[str, str] = {}


def _add(canon: str, *names: str) -> None:
    for n in names:
        CANONICAL[n.lower()] = canon


_add("Department of Defense", "dept of defense", "department of defense", "dod",
     "department of war", "dow", "defense", "esd", "whs", "dcma", "dcaa", "dla",
     "dfas", "dtra", "dodea", "dodig", "asbca", "africom", "eucom", "cybercom")
_add("Department of Justice", "justice, department of", "doj", "office of the ag",
     "office of the dag", "oip", "olc", "criminal", "civil rights", "atf", "dea",
     "fbi", "bop", "eousa", "usms", "usao")
_add("Department of Homeland Security", "homeland security, department of", "dhs",
     "uscis", "ice", "cbp", "tsa", "fema", "usss", "uscg", "dcms")
_add("Department of Veterans Affairs", "veterans affairs, department of", "va",
     "oprm", "vha", "vba")
_add("Department of the Interior", "interior, department of the", "doi", "nps",
     "bia", "bsee", "bor", "blm", "usgs", "fws", "sol")
_add("Department of the Treasury", "department of the treasury", "treasury",
     "treasury, department of the", "tigta", "irs", "fiscal service", "do")
_add("Department of Agriculture", "agriculture, department of", "usda", "ams",
     "dm", "fsis", "nrcs")
_add("Department of Health and Human Services", "health and human services",
     "hhs", "os", "phs", "acf", "ahrq", "cdc", "fda", "nih", "cms")
_add("Department of Labor", "labor, department of", "dol", "ilab", "ebsa",
     "oalj", "ofccp", "osha", "eta")
_add("Department of State", "state, department of", "dos", "ppt")
_add("Department of Transportation", "transportation, department of", "dot",
     "faa", "fhwa", "fra", "railroads", "nhtsa")
_add("Department of Energy", "energy, department of", "doe", "spr", "eia")
_add("Department of Commerce", "commerce, department of", "doc", "osec",
     "noaa", "census", "uspto")
_add("Department of Education", "education, department of", "ed")
_add("Department of Housing and Urban Development", "housing and urban development",
     "hud")
_add("General Services Administration", "general services administration", "gsa")
_add("Environmental Protection Agency", "environmental protection agency", "epa")
_add("Social Security Administration", "social security administration", "ssa")
_add("Small Business Administration", "small business administration", "sba")
_add("Council on Environmental Quality", "ceq")
_add("Council of the Inspectors General on Integrity and Efficiency", "cigie",
     "ignet")
_add("Federal Communications Commission", "fcc")
_add("Federal Deposit Insurance Corporation", "fdic")
_add("Federal Energy Regulatory Commission", "ferc")
_add("Nuclear Regulatory Commission", "nrc")
_add("National Aeronautics and Space Administration", "nasa")
_add("Court Services and Offender Supervision Agency", "csosa")
_add("Commodity Futures Trading Commission", "cftc")
_add("Appraisal Subcommittee", "asc")
_add("U.S. Agency for Global Media", "usagm", "bbg")

# "<Something> OIG" is the inspector general OF that something.
OIG_RE = re.compile(r"^(.*?)\s+(?:office of )?(?:the )?(?:inspector general|oig)\b",
                    re.I)


def canonical(raw: str) -> str:
    """A single spelling for an agency, or the original when unrecognised."""
    if not raw:
        return ""
    v = re.sub(r"\s+", " ", raw).strip().strip(".,")
    hit = CANONICAL.get(v.lower())
    if hit:
        return hit
    m = OIG_RE.match(v)
    if m:
        parent = CANONICAL.get(m.group(1).strip().lower())
        if parent:
            return parent
    # Trailing-comma form: "JUSTICE, DEPARTMENT OF"
    if "," in v:
        flipped = " ".join(reversed([p.strip() for p in v.split(",", 1)]))
        hit = CANONICAL.get(flipped.lower())
        if hit:
            return hit
    return v
