"""Cheap rule-based filters, so only plausible jobs reach the (paid) scoring step.

Location rules reject only what is clearly ineligible; anything ambiguous passes and the scorer judges it.
"""

import re
from datetime import UTC, datetime, timedelta

from .models import Job

ANYWHERE = ["worldwide", "anywhere", "global", "remote"]

REGION_TERMS = {
    "uk": ["uk", "united kingdom", "great britain", "britain", "england", "scotland", "wales",
           "northern ireland", "london", "manchester", "edinburgh", "emea"],
    "eu": ["europe", "european union", "eu", "emea", "eea", "cet", "austria", "belgium", "bulgaria",
           "croatia", "cyprus", "czechia", "czech republic", "denmark", "estonia", "finland", "france",
           "germany", "greece", "hungary", "ireland", "italy", "latvia", "lithuania", "luxembourg",
           "malta", "netherlands", "poland", "portugal", "romania", "slovakia", "slovenia", "spain",
           "sweden", "berlin", "munich", "hamburg", "amsterdam", "paris", "madrid", "barcelona",
           "lisbon", "dublin", "warsaw", "prague", "vienna", "stockholm", "copenhagen"],
    "emea": ["emea", "africa", "east africa", "uganda", "kenya", "eat", "utc+3", "gmt+3"],
}

# Lever reports countries as ISO codes, which are only safe to match case-sensitively as whole words.
REGION_CODES = {
    "uk": ["GB", "UK"],
    "eu": ["AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT",
           "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE", "EU"],
    "emea": ["UG", "KE"],
}


def _any_word(terms: list[str], text: str, flags=re.I) -> bool:
    return any(re.search(rf"\b{re.escape(t)}\b", text, flags) for t in terms)


def location_ok(job: Job, regions: list[str]) -> bool:
    text = job.location.strip()
    if not text:
        return True
    terms = [t for r in regions for t in REGION_TERMS[r]]
    codes = [c for r in regions for c in REGION_CODES[r]]
    if _any_word(terms, text) or _any_word(codes, text, 0):
        return True
    # "Remote" or "Worldwide" alone is open; "Remote, United States" is not.
    remainder = re.sub(r"\b(" + "|".join(ANYWHERE) + r")\b", "", text, flags=re.I)
    return not re.search(r"[a-z]", remainder, re.I)


def reject_reason(job: Job, search: dict, now: datetime | None = None) -> str | None:
    now = now or datetime.now(UTC)
    if not _any_word(search["title_include"], job.title):
        return "title not in scope"
    if _any_word([t.strip() for t in search.get("title_exclude", [])], job.title):
        return "title excluded"
    if search.get("remote_only") and job.remote is False:
        return "not remote"
    if not location_ok(job, search["allowed_regions"]):
        return f"location: {job.location}"
    if job.posted_at and job.posted_at < now - timedelta(days=search["max_age_days"]):
        return "too old"
    return None
