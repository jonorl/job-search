"""Scores each job against the master CV with a small, cheap model."""

import logging
import random
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

import yaml
from google import genai
from google.genai import errors, types
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class JobScore(BaseModel):
    score: int = Field(description="0-100 overall fit; 70+ means worth applying")
    fully_remote: bool = Field(
        description="False if the advert requires office days, hybrid working or relocation"
    )
    location_eligible: bool = Field(
        description="False if the job clearly cannot hire a remote worker based in the UK or the EU"
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
- The candidate wants fully remote work they can do from the UK or the EU. A job restricted to other \
countries or time zones far from Europe is not eligible.
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


def score_jobs(rows, cv: dict, cfg: dict, languages: list[str], on_scored) -> None:
    client = genai.Client()
    # Contact details don't affect fit, so they stay out of every prompt.
    cv = {k: v for k, v in cv.items() if k != "contact"}
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM.format(
            languages=", ".join(languages), cv=yaml.safe_dump(cv, sort_keys=False, allow_unicode=True)
        ),
        response_mime_type="application/json",
        response_schema=JobScore,
        thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW),
    )

    def one(row):
        for attempt in range(5):
            try:
                response = client.models.generate_content(
                    model=cfg["model"], contents=_user_prompt(row), config=config
                )
                break
            except errors.ClientError as e:
                # The free tier's per-minute limit is low, so back off on 429; other 4xx fail every job.
                if e.code != 429:
                    raise
                time.sleep(2**attempt * 5 + random.random())
            except errors.ServerError as e:
                log.warning("scoring %s failed: %r", row["id"], e)
                time.sleep(5)
        else:
            log.warning("scoring %s gave up after repeated rate limits", row["id"])
            return
        if not isinstance(response.parsed, JobScore):
            reason = response.candidates[0].finish_reason if response.candidates else "no candidates"
            log.warning("scoring %s returned nothing usable (%s)", row["id"], reason)
            return
        on_scored(row, response.parsed)

    with ThreadPoolExecutor(max_workers=cfg.get("workers", 2)) as pool:
        list(pool.map(one, rows))
