"""One fetcher per job source, each returning normalised Jobs.

Only public, documented APIs: scraping LinkedIn or Indeed breaks their terms and breaks often.
"""

import html
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree

import httpx
import yaml

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


# Company job boards. Each takes one board and raises if it's gone; `_boards` runs them over a list.
# A board is a slug, or in boards.yaml a slug with the company name, since some APIs only give the slug.


def _greenhouse(client: httpx.Client, slug: str, name: str) -> list[Job]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        location = (j.get("location") or {}).get("name", "")
        jobs.append(Job(
            source="greenhouse",
            external_id=str(j["id"]),
            title=j["title"],
            company=j.get("company_name") or name,
            url=j["absolute_url"],
            description=strip_html(j.get("content")),
            location=location,
            remote="remote" in location.lower(),
            posted_at=from_iso(j.get("first_published") or j.get("updated_at")),
        ))
    return jobs


def _lever(client: httpx.Client, slug: str, name: str, host: str = "api.lever.co", source: str = "lever") -> list[Job]:
    r = client.get(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
    r.raise_for_status()
    jobs = []
    for j in r.json():
        cats = j.get("categories") or {}
        parts = [j.get("descriptionPlain") or ""]
        parts += [f"{s['text']}\n{strip_html(s['content'])}" for s in j.get("lists") or []]
        parts.append(j.get("additionalPlain") or "")
        jobs.append(Job(
            source=source,
            external_id=j["id"],
            title=j["text"],
            company=name,
            url=j["hostedUrl"],
            apply_url=j.get("applyUrl") or "",
            description="\n\n".join(p for p in parts if p),
            location=", ".join(filter(None, [cats.get("location"), j.get("country")])),
            remote=j.get("workplaceType") == "remote",
            posted_at=from_epoch(j.get("createdAt")),
        ))
    return jobs


def _lever_eu(client: httpx.Client, slug: str, name: str) -> list[Job]:
    # EU-hosted Lever accounts live on a separate API that doesn't know about the US ones.
    return _lever(client, slug, name, host="api.eu.lever.co", source="lever_eu")


def _ashby(client: httpx.Client, slug: str, name: str) -> list[Job]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
    r.raise_for_status()
    jobs = []
    for j in r.json().get("jobs", []):
        if not j.get("isListed", True):
            continue
        country = ((j.get("address") or {}).get("postalAddress") or {}).get("addressCountry")
        secondary = [s.get("location", "") for s in j.get("secondaryLocations") or []]
        jobs.append(Job(
            source="ashby",
            external_id=j["id"],
            title=j["title"],
            company=name,
            url=j["jobUrl"],
            apply_url=j.get("applyUrl") or "",
            description=j.get("descriptionPlain") or strip_html(j.get("descriptionHtml")),
            location=", ".join(filter(None, [j.get("location"), country, *secondary])),
            remote=bool(j.get("isRemote")) or j.get("workplaceType") == "Remote",
            salary=((j.get("compensation") or {}).get("compensationTierSummary")) or "",
            posted_at=from_iso(j.get("publishedAt")),
        ))
    return jobs


def _workable(client: httpx.Client, slug: str, name: str) -> list[Job]:
    r = client.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
    r.raise_for_status()
    data = r.json()
    jobs = []
    for j in data.get("jobs", []):
        places = j.get("locations") or [j]
        location = "; ".join(", ".join(filter(None, [p.get("city"), p.get("country")])) for p in places)
        jobs.append(Job(
            source="workable",
            external_id=f"{slug}/{j['shortcode']}",
            title=j["title"],
            company=data.get("name") or name,
            url=j.get("url") or j.get("shortlink") or f"https://apply.workable.com/{slug}/j/{j['shortcode']}/",
            apply_url=j.get("application_url") or "",
            description=strip_html(j.get("description")),
            location=location,
            remote=bool(j.get("telecommuting")),
            posted_at=from_iso(j.get("published_on") or j.get("created_at")),
        ))
    return jobs


def _recruitee(client: httpx.Client, slug: str, name: str) -> list[Job]:
    r = client.get(f"https://{slug}.recruitee.com/api/offers/")
    r.raise_for_status()
    jobs = []
    for j in r.json().get("offers", []):
        jobs.append(Job(
            source="recruitee",
            external_id=f"{slug}/{j['id']}",
            title=j["title"],
            company=j.get("company_name") or name,
            url=j.get("careers_url") or "",
            apply_url=j.get("careers_apply_url") or "",
            description=strip_html(f"{j.get('description') or ''}\n{j.get('requirements') or ''}"),
            location=j.get("location") or ", ".join(filter(None, [j.get("city"), j.get("country")])),
            remote=bool(j.get("remote")),
            posted_at=from_iso((j.get("published_at") or "").replace(" UTC", "Z").replace(" ", "T") or None),
        ))
    return jobs


def _personio(client: httpx.Client, slug: str, name: str) -> list[Job]:
    r = client.get(f"https://{slug}.jobs.personio.de/xml", params={"language": "en"})
    r.raise_for_status()
    jobs = []
    for p in ElementTree.fromstring(r.content).iter("position"):
        text = lambda tag: (p.findtext(tag) or "").strip()
        offices = [text("office"), *[(o.text or "").strip() for o in p.iter("additionalOffice")]]
        location = ", ".join(filter(None, offices))
        description = "\n\n".join(
            f"{d.findtext('name') or ''}\n{strip_html(d.findtext('value'))}" for d in p.iter("jobDescription"))
        jobs.append(Job(
            source="personio",
            external_id=f"{slug}/{text('id')}",
            title=text("name"),
            company=text("subcompany") or name,
            url=f"https://{slug}.jobs.personio.de/job/{text('id')}",
            description=description,
            location=location,
            # Personio has no remote flag; companies put it in the office name instead.
            remote=True if "remote" in location.lower() else None,
            posted_at=from_iso(text("createdAt") or None),
        ))
    return jobs


def _teamtailor(client: httpx.Client, slug: str, name: str) -> list[Job]:
    # Many Teamtailor sites sit on the company's own domain, so the board can be a full host.
    host = slug if "." in slug else f"{slug}.teamtailor.com"
    r = client.get(f"https://{host}/jobs.rss")
    r.raise_for_status()
    root = ElementTree.fromstring(r.content)
    company = name or (root.findtext("channel/title") or slug)
    ns = {"tt": "https://teamtailor.com/locations"}
    jobs = []
    for item in root.iter("item"):
        places = [", ".join(filter(None, [loc.findtext("tt:city", "", ns), loc.findtext("tt:country", "", ns)]))
                  for loc in item.findall("tt:locations/tt:location", ns)]
        status = item.findtext("remoteStatus") or ""
        jobs.append(Job(
            source="teamtailor",
            external_id=f"{host}/{item.findtext('guid')}",
            title=(item.findtext("title") or "").strip(),
            company=company,
            url=item.findtext("link") or "",
            description=strip_html(item.findtext("description")),
            location="; ".join(filter(None, places)) or ("Remote" if status == "fully" else ""),
            remote=status == "fully" if status else None,
            posted_at=parsedate_to_datetime(item.findtext("pubDate")) if item.findtext("pubDate") else None,
        ))
    return jobs


def _smartrecruiters(client: httpx.Client, slug: str, name: str) -> list[Job]:
    jobs, offset = [], 0
    while True:
        r = client.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                       params={"limit": 100, "offset": offset})
        r.raise_for_status()
        data = r.json()
        for j in data.get("content", []):
            loc = j.get("location") or {}
            jobs.append(Job(
                source="smartrecruiters",
                external_id=f"{slug}/{j['id']}",
                title=j["name"],
                company=(j.get("company") or {}).get("name") or name,
                url=f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                # The list has no advert text; enrich fetches it for the jobs that pass the filters.
                description="",
                location=loc.get("fullLocation") or ", ".join(filter(None, [loc.get("city"), loc.get("country")])),
                remote=bool(loc.get("remote")),
                posted_at=from_iso(j.get("releasedDate")),
            ))
        offset += 100
        if offset >= data.get("totalFound", 0):
            return jobs


def smartrecruiters_details(client: httpx.Client, external_id: str) -> dict:
    slug, job_id = external_id.split("/", 1)
    r = client.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{job_id}")
    r.raise_for_status()
    data = r.json()
    sections = ((data.get("jobAd") or {}).get("sections") or {}).values()
    text = "\n\n".join(f"{s.get('title') or ''}\n{strip_html(s.get('text'))}" for s in sections if s.get("text"))
    return {"description": text, "apply_url": data.get("applyUrl")}


BOARDS = {
    "greenhouse": _greenhouse,
    "lever": _lever,
    "lever_eu": _lever_eu,
    "ashby": _ashby,
    "workable": _workable,
    "recruitee": _recruitee,
    "personio": _personio,
    "teamtailor": _teamtailor,
    "smartrecruiters": _smartrecruiters,
}


def _boards(source: str):
    def fetch(client: httpx.Client, boards: list | dict) -> list[Job]:
        boards = boards if isinstance(boards, dict) else {slug: slug for slug in boards}
        # Hundreds of small boards one after another would take most of the run.
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = pool.map(lambda b: _board(client, source, *b), boards.items())
            found = [r for r in results if r is not None]
        if len(found) < len(boards):
            log.warning("%s: %d of %d boards failed", source, len(boards) - len(found), len(boards))
        return [j for jobs in found for j in jobs]
    return fetch


def _board(client: httpx.Client, source: str, slug: str, name: str | None) -> list[Job] | None:
    try:
        return BOARDS[source](client, slug, name or slug)
    except (httpx.HTTPError, ValueError, KeyError, ElementTree.ParseError) as e:
        # Boards close or move; one gone shouldn't take the rest of the source with it.
        log.info("%s board %s failed: %r", source, slug, e)
        return None


def load_boards(path: Path) -> dict[str, dict[str, str]]:
    return yaml.safe_load(path.read_text()) or {} if path.exists() else {}


def with_boards(sources_cfg: dict, boards: dict[str, dict[str, str]]) -> dict:
    """The sources config with discovered boards added; a source set to false in the config stays off."""
    merged = dict(sources_cfg)
    for source, found in boards.items():
        own = sources_cfg.get(source)
        if own is False:
            continue
        own = own if isinstance(own, dict) else {slug: slug for slug in own or []}
        merged[source] = {**found, **own}
    return merged


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
    **{name: _boards(name) for name in BOARDS},
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
ENRICHERS = {"reed": reed_details, "smartrecruiters": smartrecruiters_details}


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
