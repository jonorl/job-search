"""Scores each job against the master CV with a small, cheap model."""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .llm import generate

log = logging.getLogger(__name__)


class JobScore(BaseModel):
    score: int = Field(description="0-100 overall fit; 70+ means worth applying")
    fully_remote: bool = Field(
        description="False if the advert requires office days, hybrid working or relocation"
    )
    location_eligible: bool = Field(
        description="False if the job requires living somewhere the candidate does not, or working hours far from theirs"
    )
    language_ok: bool = Field(description="False if the job requires a language the candidate does not speak")
    seniority_fit: Literal["under", "match", "over"] = Field(
        description="Job level relative to the candidate: under = too junior for them, over = too senior"
    )
    reasons: list[str] = Field(description="Up to 3 short reasons it fits, citing CV facts")
    gaps: list[str] = Field(description="Up to 3 short requirements the CV does not show")


SYSTEM = """You screen job adverts for one candidate. Judge fit using only the CV below; never assume \
skills or experience it does not state.

Rules:
- The candidate wants fully remote work. They are based in {based_in}, and can work UK and EU hours.
- Eligible: jobs open worldwide or across EMEA/Africa, and UK or EU jobs that hire contractors or \
employees abroad (e.g. through an employer of record). Not eligible: jobs that require living in a \
particular country the candidate is not in, or hours far from theirs (e.g. US time zones only).
- The candidate speaks: {languages}.
- Their software-engineering experience is recent (they moved from 10+ years in BI and data); weigh \
years of professional software experience accordingly when the advert asks for them.
- Some adverts are only a snippet. Judge what is there; don't mark a job down for detail it omits.
- Score 0-100. A job that is hybrid or office-based, not location-eligible, or needs a language they \
lack scores 20 or less.

<cv>
{cv}
</cv>"""


def _user_prompt(row) -> str:
    return f"""<job>
Title: {row["title"]}
Company: {row["company"]}
Location: {row["location"] or "not stated"}
Salary: {row["salary"] or "not stated"}

{row["description"]}
</job>"""


def score_jobs(rows, cv: dict, cfg: dict, search: dict, on_scored) -> None:
    # Contact details don't affect fit, so they stay out of every prompt.
    cv = {k: v for k, v in cv.items() if k != "contact"}
    system = SYSTEM.format(
        languages=", ".join(search["languages"]),
        based_in=search["based_in"],
        cv=yaml.safe_dump(cv, sort_keys=False, allow_unicode=True),
    )

    def one(row):
        result = generate(cfg["model"], system, _user_prompt(row), JobScore)
        if result is None:
            log.warning("could not score %s", row["id"])
            return
        on_scored(row, result)

    with ThreadPoolExecutor(max_workers=cfg.get("workers", 2)) as pool:
        list(pool.map(one, rows))
