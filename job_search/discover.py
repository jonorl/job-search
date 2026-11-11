"""Finds company job boards to watch directly, so jobs turn up before the aggregators repost them.

The companies come from remoteintech/remote-jobs, a crowd-kept list of remote-friendly firms. Each
careers page is read for links to a known applicant-tracking system, and every board found is checked
against that system's API before it's kept. The result goes to boards.yaml, which `fetch` reads beside
config.yaml; rerun `job-search discover` now and then rather than editing it by hand.
"""

import io
import logging
import re
import tarfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import yaml

from .applications import company_key
from .sources import BOARDS, HEADERS

log = logging.getLogger(__name__)

LIST = "https://codeload.github.com/remoteintech/remote-jobs/tar.gz/refs/heads/main"
# Regions the list uses that can include someone working from East Africa; "other" is mostly unstated.
REGIONS = {"worldwide", "europe", "americas-europe", "other"}
# Slugs that are parts of the systems' own sites rather than a company's board.
NOT_SLUGS = {"www", "app", "api", "apply", "jobs", "careers", "career", "j", "embed", "assets", "static",
             "cdn", "help", "support", "blog", "status", "oneclick-ui", "boards", "job-boards", "v1", "en", "de"}
PATTERNS = {
    "greenhouse": [r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board(?:/js)?\?for=)?([a-z0-9_-]+)",
                   r"boards-api\.greenhouse\.io/v1/boards/([a-z0-9_-]+)"],
    "lever": [r"jobs\.lever\.co/([a-z0-9._-]+)"],
    "lever_eu": [r"jobs\.eu\.lever\.co/([a-z0-9._-]+)"],
    "ashby": [r"jobs\.ashbyhq\.com/([a-z0-9._-]+)"],
    "workable": [r"apply\.workable\.com/([a-z0-9-]+)", r"//([a-z0-9-]+)\.workable\.com"],
    "recruitee": [r"//([a-z0-9-]+)\.recruitee\.com"],
    "personio": [r"//([a-z0-9-]+)\.jobs\.personio\.(?:de|com)"],
    "teamtailor": [r"//([a-z0-9-]+)\.teamtailor\.com"],
    "smartrecruiters": [r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"],
}


def companies(client: httpx.Client) -> list[dict]:
    """The list's company profiles, read from the repository archive without unpacking it."""
    r = client.get(LIST, timeout=120)
    r.raise_for_status()
    found = []
    with tarfile.open(fileobj=io.BytesIO(r.content)) as archive:
        for member in archive.getmembers():
            if not (member.isfile() and "/src/companies/" in member.name and member.name.endswith(".md")):
                continue
            parts = archive.extractfile(member).read().decode("utf-8", "replace").split("---", 2)
            try:
                profile = yaml.safe_load(parts[1]) if len(parts) == 3 else None
            except yaml.YAMLError:
                continue
            if isinstance(profile, dict) and profile.get("title"):
                found.append(profile)
    return found


def wanted(profile: dict) -> bool:
    return profile.get("region") in REGIONS and profile.get("remote_policy") != "hybrid"


def board_links(page: str) -> set[tuple[str, str]]:
    found = set()
    for source, patterns in PATTERNS.items():
        for pattern in patterns:
            for slug in re.findall(pattern, page, re.I):
                slug = slug.strip(".-_").lower() if source != "smartrecruiters" else slug.strip(".-_")
                if slug and slug.lower() not in NOT_SLUGS:
                    found.add((source, slug))
    # EU Lever links also match the US pattern's tail, so drop the US copy.
    return {b for b in found if not (b[0] == "lever" and ("lever_eu", b[1]) in found)}


def _get(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        r = client.get(url)
        return r if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


def careers_page_boards(client: httpx.Client, profile: dict) -> set[tuple[str, str]]:
    start = profile.get("careers_url") or profile.get("website")
    if not start or not str(start).startswith("http"):
        return set()
    found = board_links(str(start))
    if found:
        return found
    r = _get(client, str(start))
    if r is None:
        return set()
    found = board_links(r.text)
    if "teamtailor" in r.text and not any(s == "teamtailor" for s, _ in found):
        # A Teamtailor site on the company's own domain has its feed on that domain.
        found.add(("teamtailor", r.url.host))
    if found:
        return found
    # A home page usually links to the careers page, which is where the board is embedded.
    site = r.url.host
    for href in re.findall(r'href="([^"#]+)"', r.text)[:400]:
        url = urljoin(str(r.url), href)
        if urlparse(url).hostname == site and re.search(r"career|jobs|join|work-with-us|hiring", url, re.I):
            inner = _get(client, url)
            if inner is not None:
                found = board_links(inner.text)
                if "teamtailor" in inner.text and not found:
                    found.add(("teamtailor", inner.url.host))
            break
    return found


def check(client: httpx.Client, source: str, slug: str, name: str) -> int | None:
    """How many jobs the board has open, or None if it doesn't answer as that system's board."""
    try:
        return len(BOARDS[source](client, slug, name))
    except Exception:
        return None


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", company_key(text))


def guessed_boards(client: httpx.Client, profile: dict) -> set[tuple[str, str]]:
    """Boards at the company's own slug, for careers pages that load their jobs with JavaScript."""
    name = str(profile["title"])
    slugs = {str(profile.get("slug") or ""), _squash(name), re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")}
    found = set()
    for slug in filter(None, slugs):
        # Greenhouse names its boards, which settles whose it is.
        board = _get(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        if board is not None and _squash(board.json().get("name", "")) == _squash(name):
            found.add(("greenhouse", slug))
        # Lever and Ashby don't, so the company has to be named in its own adverts; a firm that
        # shares a slug with someone else's board won't be.
        for source in ("lever", "ashby"):
            try:
                jobs = BOARDS[source](client, slug, name)
            except Exception:
                continue
            text = _squash(" ".join(j.description[:3000] for j in jobs[:5]))
            if jobs and _squash(name) in text:
                found.add((source, slug))
    return found


def discover(path: Path) -> dict[str, dict[str, str]]:
    with httpx.Client(headers=HEADERS, timeout=20, follow_redirects=True) as client:
        profiles = [p for p in companies(client) if wanted(p)]
        log.info("%d companies in eligible regions; reading their careers pages", len(profiles))

        def one(profile):
            name = str(profile["title"])
            kept = []
            for source, slug in careers_page_boards(client, profile) or guessed_boards(client, profile):
                if check(client, source, slug, name) is not None:
                    kept.append((source, slug, name))
            return kept

        with ThreadPoolExecutor(max_workers=16) as pool:
            results = [b for found in pool.map(one, profiles) for b in found]

    boards: dict[str, dict[str, str]] = {}
    for source, slug, name in sorted(results):
        boards.setdefault(source, {})[slug] = name
    _write(path, boards)
    log.info("kept %d boards: %s", len(results), ", ".join(f"{s} {len(b)}" for s, b in sorted(boards.items())))
    return boards


def _write(path: Path, boards: dict) -> None:
    header = (f"# Written by `job-search discover` on {date.today().isoformat()} from remoteintech/remote-jobs.\n"
              "# Rerun it to refresh rather than editing this; hand-picked boards go in config.yaml.\n"
              "# A source set to false in config.yaml switches its boards here off too.\n")
    path.write_text(header + yaml.safe_dump(boards, sort_keys=True, allow_unicode=True))
