"""One fetcher per job source, each returning normalised Jobs.

Only public, documented APIs: scraping LinkedIn or Indeed breaks their terms and breaks often.
"""

import html
import logging
import os
import re
from datetime import UTC, datetime

import httpx

from .models import Job

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "job-search/0.1 (personal use; github.com/jonorl)"}


def strip_html(text: str | None) -> str:
    text = html.unescape(text or "")
    # Greenhouse double-escapes, so a second pass is needed for its entities.
    text = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def from_epoch(value) -> datetime | None:
    if not value:
        return None
    value = int(value)
    # Lever uses milliseconds, everyone else seconds.
    return datetime.fromtimestamp(value / 1000 if value > 10**11 else value, UTC)


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def remotive(client: httpx.Client, _cfg) -> list[Job]:
    # Remotive's terms ask for at most four calls a day and a link back, which the job URL provides.
    data = client.get("https://remotive.com/api/remote-jobs", params={"category": "software-dev"}).json()
    return [
        Job(
            source="remotive",
            external_id=str(j["id"]),
            title=j["title"],
            company=j["company_name"],
            url=j["url"],
            description=strip_html(j["description"]),
            location=j.get("candidate_required_location") or "",
            remote=True,
            salary=j.get("salary") or "",
            posted_at=from_iso(j.get("publication_date")),
            tags=j.get("tags") or [],
        )
        for j in data["jobs"]
    ]


def remoteok(client: httpx.Client, _cfg) -> list[Job]:
    data = client.get("https://remoteok.com/api").json()
    jobs = []
    # The first element is RemoteOK's legal notice, not a job.
    for j in data[1:]:
        salary = ""
        if j.get("salary_min") and j.get("salary_max"):
            salary = f"${int(j['salary_min']):,}–${int(j['salary_max']):,}"
        jobs.append(Job(
            source="remoteok",
            external_id=str(j["id"]),
            title=j["position"],
            company=j["company"],
            url=j["url"],
            apply_url=j.get("apply_url") or "",
            description=strip_html(j.get("description")),
            location=j.get("location") or "",
            remote=True,
            salary=salary,
            posted_at=from_iso(j.get("date")),
            tags=j.get("tags") or [],
        ))
    return jobs


def himalayas(client: httpx.Client, cfg) -> list[Job]:
    # The browse feed has 100k+ jobs at 20 a page, so search instead; country=GB also returns worldwide jobs.
    jobs = []
    for query in cfg.get("queries", ["software engineer"]):
        for country in cfg.get("countries", ["GB"]):
            for page in range(1, cfg.get("pages", 3) + 1):
                data = client.get(
                    "https://himalayas.app/jobs/api/search",
                    params={"q": query, "country": country, "sort": "recent", "page": page},
                ).json()
                for j in data["jobs"]:
                    salary = ""
                    if j.get("minSalary") and j.get("maxSalary"):
                        salary = f"{j.get('currency') or ''} {int(j['minSalary']):,}–{int(j['maxSalary']):,}".strip()
                    jobs.append(Job(
                        source="himalayas",
                        external_id=j["guid"],
                        title=j["title"],
                        company=j["companyName"],
                        url=j["applicationLink"],
                        description=strip_html(j.get("description")),
                        location=", ".join(j.get("locationRestrictions") or []) or "Worldwide",
                        remote=True,
                        salary=salary,
                        posted_at=from_epoch(j.get("pubDate")),
                        tags=j.get("categories") or [],
                    ))
                if not data["jobs"]:
                    break
    return jobs


def arbeitnow(client: httpx.Client, cfg) -> list[Job]:
    jobs = []
    for page in range(1, cfg.get("pages", 5) + 1):
        data = client.get("https://www.arbeitnow.com/api/job-board-api", params={"page": page}).json()
        for j in data["data"]:
            jobs.append(Job(
                source="arbeitnow",
                external_id=j["slug"],
                title=j["title"],
                company=j["company_name"],
                url=j["url"],
                description=strip_html(j.get("description")),
                # Arbeitnow is a German board, so an unqualified location is in Germany.
                location=f"{j.get('location') or ''}, Germany".lstrip(", "),
                remote=bool(j.get("remote")),
                posted_at=from_epoch(j.get("created_at")),
                tags=j.get("tags") or [],
            ))
        if not data["links"].get("next"):
            break
    return jobs


def greenhouse(client: httpx.Client, slugs: list[str]) -> list[Job]:
    jobs = []
    for slug in slugs:
        data = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"}).json()
        for j in data.get("jobs", []):
            location = (j.get("location") or {}).get("name", "")
            jobs.append(Job(
                source="greenhouse",
                external_id=str(j["id"]),
                title=j["title"],
                company=j.get("company_name") or slug,
                url=j["absolute_url"],
                description=strip_html(j.get("content")),
                location=location,
                remote="remote" in location.lower(),
                posted_at=from_iso(j.get("first_published") or j.get("updated_at")),
            ))
    return jobs


def lever(client: httpx.Client, slugs: list[str]) -> list[Job]:
    jobs = []
    for slug in slugs:
        for j in client.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}).json():
            cats = j.get("categories") or {}
            parts = [j.get("descriptionPlain") or ""]
            parts += [f"{s['text']}\n{strip_html(s['content'])}" for s in j.get("lists") or []]
            parts.append(j.get("additionalPlain") or "")
            jobs.append(Job(
                source="lever",
                external_id=j["id"],
                title=j["text"],
                company=slug,
                url=j["hostedUrl"],
                apply_url=j.get("applyUrl") or "",
                description="\n\n".join(p for p in parts if p),
                location=", ".join(filter(None, [cats.get("location"), j.get("country")])),
                remote=j.get("workplaceType") == "remote",
                posted_at=from_epoch(j.get("createdAt")),
            ))
    return jobs


def ashby(client: httpx.Client, slugs: list[str]) -> list[Job]:
    jobs = []
    for slug in slugs:
        data = client.get(
            f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"}
        ).json()
        for j in data.get("jobs", []):
            if not j.get("isListed", True):
                continue
            country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
            secondary = [s.get("location", "") for s in j.get("secondaryLocations") or []]
            jobs.append(Job(
                source="ashby",
                external_id=j["id"],
                title=j["title"],
                company=slug,
                url=j["jobUrl"],
                apply_url=j.get("applyUrl") or "",
                description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
                location=", ".join(filter(None, [j.get("location"), country, *secondary])),
                remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
                salary=((j.get("compensation") or {}).get("compensationTierSummary")) or "",
                posted_at=from_iso(j.get("publishedAt")),
            ))
    return jobs


def uk_location(name: str | None) -> str:
    # UK-only boards give bare towns like "Leeds", which the region filter wouldn't recognise.
    name = (name or "").strip()
    return name if re.search(r"\b(UK|United Kingdom)$", name) else f"{name}, UK".lstrip(", ")


def _env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is not set")
    return value


def reed(client: httpx.Client, cfg) -> list[Job]:
    # Reed has no remote flag, so "remote" goes in the keywords and the scorer checks the advert itself.
    auth = (_env("REED_API_KEY"), "")
    jobs = []
    for query in cfg.get("queries", ["software engineer"]):
        for page in range(cfg.get("pages", 2)):
            results = client.get(
                "https://www.reed.co.uk/api/1.0/search",
                params={"keywords": f"{query} remote", "resultsToTake": 100, "resultsToSkip": page * 100},
                auth=auth,
            ).json()["results"]
            for j in results:
                salary = ""
                if j.get("minimumSalary") and j.get("maximumSalary"):
                    salary = f"£{int(j['minimumSalary']):,}–£{int(j['maximumSalary']):,}"
                jobs.append(Job(
                    source="reed",
                    external_id=str(j["jobId"]),
                    title=j["jobTitle"],
                    company=j.get("employerName") or "",
                    url=j["jobUrl"],
                    # Only a snippet; reed_details fetches the full advert for jobs that pass the filters.
                    description=strip_html(j.get("jobDescription")),
                    location=uk_location(j.get("locationName")),
                    salary=salary,
                    posted_at=datetime.strptime(j["date"], "%d/%m/%Y").replace(tzinfo=UTC) if j.get("date") else None,
                ))
            if len(results) < 100:
                break
    return jobs


def reed_details(client: httpx.Client, job_id: str) -> dict:
    j = client.get(f"https://www.reed.co.uk/api/1.0/jobs/{job_id}", auth=(_env("REED_API_KEY"), "")).json()
    return {"description": strip_html(j.get("jobDescription")), "apply_url": j.get("externalUrl") or j.get("jobUrl")}


def adzuna(client: httpx.Client, cfg) -> list[Job]:
    # Adzuna only ever returns a snippet of the advert; the scorer is told so.
    creds = {"app_id": _env("ADZUNA_APP_ID"), "app_key": _env("ADZUNA_APP_KEY")}
    jobs = []
    for query in cfg.get("queries", ["software engineer"]):
        for page in range(1, cfg.get("pages", 2) + 1):
            results = client.get(
                f"https://api.adzuna.com/v1/api/jobs/gb/search/{page}",
                params={**creds, "what": f"{query} remote", "results_per_page": 50, "sort_by": "date",
                        "max_days_old": cfg.get("max_days_old", 14)},
            ).json()["results"]
            for j in results:
                salary = ""
                # Adzuna estimates salaries it isn't given; an estimate would mislead the shortlist.
                if j.get("salary_min") and j.get("salary_max") and str(j.get("salary_is_predicted")) != "1":
                    salary = f"£{int(j['salary_min']):,}–£{int(j['salary_max']):,}"
                jobs.append(Job(
                    source="adzuna",
                    external_id=str(j["id"]),
                    title=strip_html(j["title"]),
                    company=(j.get("company") or {}).get("display_name", ""),
                    url=j["redirect_url"],
                    description=strip_html(j.get("description")) + "\n\n[Snippet only; full advert at the link.]",
                    location=uk_location((j.get("location") or {}).get("display_name")),
                    salary=salary,
                    posted_at=from_iso(j.get("created")),
                ))
            if len(results) < 50:
                break
    return jobs


FETCHERS = {
    "reed": reed,
    "adzuna": adzuna,
    "remotive": remotive,
    "remoteok": remoteok,
    "himalayas": himalayas,
    "arbeitnow": arbeitnow,
    "greenhouse": greenhouse,
    "lever": lever,
    "ashby": ashby,
}


def fetch_all(sources_cfg: dict) -> list[Job]:
    jobs: list[Job] = []
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        for name, cfg in sources_cfg.items():
            if not cfg:
                continue
            try:
                found = FETCHERS[name](client, {} if cfg is True else cfg)
            except (httpx.HTTPError, ValueError, KeyError) as e:
                # One broken source shouldn't sink the daily run.
                log.warning("%s failed: %r", name, e)
                continue
            log.info("%s: %d jobs", name, len(found))
            jobs.extend(found)
    return jobs


# Sources whose search results are snippets, with a call that returns the full advert.
ENRICHERS = {"reed": reed_details}


def enrich(jobs: list[Job]) -> dict[str, dict]:
    """Full adverts for the given jobs, keyed by job id. Called only for jobs that passed the filters."""
    details = {}
    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        for job in jobs:
            if job.source not in ENRICHERS:
                continue
            try:
                details[job.id] = ENRICHERS[job.source](client, job.external_id)
            except (httpx.HTTPError, ValueError) as e:
                log.warning("details for %s failed: %r", job.id, e)
    return details
