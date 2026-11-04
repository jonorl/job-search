"""Writes a tailored CV and cover letter for a shortlisted job.

The model only chooses and rewords: every project, skill and role is taken from the master CV by
reference, and a separate fact-check reverts any reworded line that claims more than its source.
"""

import json
import logging
import re
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from .llm import generate
from .render import render

log = logging.getLogger(__name__)


class ProjectPick(BaseModel):
    name: str = Field(description="Exact project name from the master CV")
    description: str = Field(description="At most 20 words")


class SkillGroup(BaseModel):
    group: str = Field(description="Exact group name from the master CV")
    items: list[str] = Field(description="Items copied exactly from that group, most relevant first")


class BulletPick(BaseModel):
    source: str = Field(description="Id of the master bullet this is based on, e.g. e0.b2")
    text: str


class RolePick(BaseModel):
    role: int = Field(description="Role id from the master CV, e.g. 0 for e0")
    bullets: list[BulletPick]


class TailoredCV(BaseModel):
    summary: str
    projects: list[ProjectPick]
    skills: list[SkillGroup]
    experience: list[RolePick]


class CoverLetter(BaseModel):
    greeting: str
    paragraphs: list[str]


class Problem(BaseModel):
    id: str = Field(description="Id of the line with the problem")
    issue: str = Field(description="What it claims that the CV does not support")


class FactCheck(BaseModel):
    problems: list[Problem]


# The model's favourite inflation, so every prompt states it outright.
EXPERIENCE = """The candidate's 10+ years are in BI and data. Their software-engineering experience is the \
current role (since Aug 2026) and their personal projects; never describe it as 10+ years, or as \
"over a decade", of software engineering."""

TAILOR_SYSTEM = f"""You tailor a CV to one job advert. Write in British English, with British spelling \
(utilise, organise, modelling).

The master CV is the only source of facts. You may select, reorder, shorten and lightly reword; you \
must never add a skill, tool, number, responsibility, outcome, scope or quality it does not state \
(e.g. don't turn "dashboard" into "user interfaces", or "tools" into "interactive tools"). Keep every \
number exactly as written. When the original wording already fits, keep it.

{EXPERIENCE}

- summary: 2-3 sentences aimed at this role, built from the master summary and experience.
- projects: the 4-6 most relevant, by exact name, each described in at most 20 words.
- skills: keep the group names; within each, copy items exactly, most relevant first, dropping the \
irrelevant; at most 10 items per group; leave out a group with nothing relevant.
- experience: one entry per role id, every role included. Pick the bullets that best match the advert \
(at least one per role) and adjust their emphasis towards what the advert asks for. Give each bullet \
the id of the master bullet it comes from. At most 14 bullets in total, so the CV fits one A4 page."""

LETTER_SYSTEM = f"""You write a cover letter for one job, in British English, from the candidate's CV.

- 230-330 words in 3 or 4 paragraphs; plain, specific and confident.
- Greeting: "Dear <name>," if the advert names the hiring manager, otherwise "Dear <company> team,", \
using the company's everyday name without suffixes such as LLC, Ltd or GmbH.
- Open with why this candidate suits this role, not by restating the advert.
- Link 2 or 3 concrete requirements from the advert to specific evidence in the CV.
- Only state facts from the CV. That includes ways of working and qualities: don't claim things like \
"maintainable code", "responsive design" or "working asynchronously" unless the CV says them. About \
the company, only use what the advert says.
- {EXPERIENCE}
- Do not mention career breaks, gaps, where the candidate lives, relocation, visas or salary.
- No em dashes. Avoid stock phrases such as "I am writing to express my interest", "passionate", \
"thrilled", "perfect fit".
- No sign-off or name at the end; the template adds them."""

CHECK_SYSTEM = f"""You are a strict fact-checker for a job application. Each line must be backed by the \
candidate's master CV.

Report a line if it claims anything about the candidate that the master CV does not state: a skill, \
tool, practice, way of working, quality, number, responsibility, outcome, employer, scope or amount of \
experience. Soft claims count too ("responsive design", "maintainable code", "working \
asynchronously"). {EXPERIENCE} Statements about the hiring company or the role are fine if the \
advert supports them. Reword-only changes are fine. Report nothing if every line is supported."""

REWRITE_SYSTEM = """You are a strict fact-checker. Each item pairs an original CV line with a rewrite.

Report an item if the rewrite says anything the original does not: a new skill, tool, practice, \
quality, scope, number, outcome or responsibility, or a broader or more impressive version of one \
(e.g. "dashboard" becoming "user interfaces", "tools" becoming "interactive tools", "led" where the \
original says "worked on"). Dropping detail, reordering and changing emphasis are fine. Report \
nothing if every rewrite is faithful."""


def _master_with_ids(cv: dict) -> str:
    """The master CV with ids the model can cite, minus contact details it doesn't need."""
    lines = [f"summary: {cv['summary']}", "", "projects:"]
    lines += [f"- {p['name']} ({', '.join(p['stack'])}): {p['description']}" for p in cv["projects"]]
    lines += ["", "skills:"]
    lines += [f"- {group}: {', '.join(items)}" for group, items in cv["skills"].items()]
    lines += ["", "experience:"]
    for i, role in enumerate(cv["experience"]):
        lines.append(f"e{i}: {role['title']}, {role['company']} ({role['start']} – {role['end']})")
        lines += [f"  e{i}.b{j}: {b}" for j, b in enumerate(role["bullets"])]
    lines += ["", "education:"]
    lines += [f"- {e['institution']}: {e['degree']} ({e['year']})" for e in cv["education"]]
    return "\n".join(lines)


def _advert(row) -> str:
    return f"""<advert>
Title: {row["title"]}
Company: {row["company"]}
Location: {row["location"] or "not stated"}

{row["description"]}
</advert>"""


def _assemble(cv: dict, t: TailoredCV) -> tuple[dict, dict[str, tuple[str, str]]]:
    """Builds the render data from the model's picks, keeping only what exists in the master CV.

    Also returns each reworded line by id, with the master text it may fall back to.
    """
    lines: dict[str, tuple[str, str]] = {"summary": (t.summary, cv["summary"])}

    remaining = list(cv["projects"])
    projects = []
    for pick in t.projects:
        # Models sometimes append the stack or a colon to the name, so match on the master name's prefix.
        picked = re.sub(r"\W+", " ", pick.name.lower()).strip()
        master = next((p for p in remaining if picked.startswith(re.sub(r"\W+", " ", p["name"].lower()).strip())), None)
        if master is None:
            log.warning("dropping unknown project %r", pick.name)
            continue
        remaining.remove(master)
        projects.append({**master, "description": pick.description})
        lines[f"p:{master['name']}"] = (pick.description, master["description"])
    if len(projects) < 3:
        log.warning("only %d projects matched; using the master list", len(projects))
        projects = cv["projects"]

    skills = []
    for g in t.skills:
        master_items = {i.lower(): i for i in cv["skills"].get(g.group, [])}
        items = [master_items[i.lower()] for i in g.items if i.lower() in master_items]
        if items:
            skills.append({"group": g.group, "items": list(dict.fromkeys(items))})
    if not skills:
        skills = [{"group": k, "items": v} for k, v in cv["skills"].items()]

    picks = {p.role: p.bullets for p in t.experience}
    experience = []
    for i, role in enumerate(cv["experience"]):
        bullets = []
        for b in picks.get(i, []):
            m = re.fullmatch(rf"e{i}\.b(\d+)", b.source.strip())
            if m and int(m.group(1)) < len(role["bullets"]):
                line_id = f"e{i}.b{m.group(1)}.{len(bullets)}"
                lines[line_id] = (b.text, role["bullets"][int(m.group(1))])
                bullets.append(line_id)
        experience.append({**role, "bullets": bullets or list(role["bullets"])})

    data = {
        "name": cv["name"],
        "contact": cv["contact"],
        "summary": t.summary,
        "projects": projects,
        "skills": skills,
        "experience": experience,
        "education": cv["education"],
    }
    return data, lines


def _report(model: list[str], system: str, prompt: str, ids) -> dict[str, str]:
    result = generate(model, system, prompt, FactCheck, thinking="MEDIUM")
    if result is None:
        # An unchecked application is worse than a plain one, so treat every line as suspect.
        return {k: "fact-check failed" for k in ids}
    return {p.id: p.issue for p in result.problems if p.id in ids}


def _check(model: list[str], cv_text: str, advert: str, lines: dict[str, str]) -> dict[str, str]:
    """Free-standing claims (the summary, the letter) against the whole CV."""
    listing = "\n".join(f"[{k}] {v}" for k, v in lines.items())
    prompt = f"<master_cv>\n{cv_text}\n</master_cv>\n\n{advert}\n\n<lines>\n{listing}\n</lines>"
    return _report(model, CHECK_SYSTEM, prompt, lines)


def _check_rewrites(model: list[str], pairs: dict[str, tuple[str, str]]) -> dict[str, str]:
    """Reworded CV lines against the one line each came from, which is far stricter than the whole CV."""
    listing = "\n\n".join(f"[{k}]\noriginal: {master}\nrewrite: {text}" for k, (text, master) in pairs.items())
    return _report(model, REWRITE_SYSTEM, f"<items>\n{listing}\n</items>", pairs)


def _letter(model: list[str], cv_text: str, advert: str, feedback: str = "") -> CoverLetter | None:
    prompt = f"<cv>\n{cv_text}\n</cv>\n\n{advert}"
    if feedback:
        prompt += f"\n\nA previous draft made claims the CV does not support. Avoid these:\n{feedback}"
    return generate(model, LETTER_SYSTEM, prompt, CoverLetter, thinking="MEDIUM")


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].rstrip("-")


def tailor(row, cv: dict, cfg: dict, out_root: Path) -> Path | None:
    model, check_model = cfg["models"], cfg["check_models"]
    cv_text, advert = _master_with_ids(cv), _advert(row)

    picks = generate(model, TAILOR_SYSTEM, f"<master_cv>\n{cv_text}\n</master_cv>\n\n{advert}", TailoredCV,
                     thinking="MEDIUM")
    if picks is None:
        return None
    data, lines = _assemble(cv, picks)
    log.info("%s: CV drafted, checking %d lines", row["id"], len(lines))

    stacks = {f"p:{p['name']}": ", ".join(p["stack"]) for p in cv["projects"]}
    # The stack is printed beside each project, so a description that names it adds nothing.
    rewrites = {
        k: (text, f"{master} [stack: {stacks[k]}]" if k in stacks else master)
        for k, (text, master) in lines.items() if k != "summary"
    }
    reverted = _check_rewrites(check_model, rewrites)
    reverted |= _check(check_model, cv_text, advert, {"summary": lines["summary"][0]})
    for line_id, issue in reverted.items():
        log.info("%s: reverting %s (%s)", row["id"], line_id, issue)
    final = {k: (master if k in reverted else text) for k, (text, master) in lines.items()}
    data["summary"] = final["summary"]
    for p in data["projects"]:
        p["description"] = final.get(f"p:{p['name']}", p["description"])
    for role in data["experience"]:
        role["bullets"] = [final.get(b, b) for b in role["bullets"]]

    # The letter is written from the checked CV, so it can't build on a reverted claim.
    letter_facts = yaml.safe_dump({k: v for k, v in data.items() if k != "contact"}, sort_keys=False,
                                  allow_unicode=True)
    best: tuple[CoverLetter, dict[str, str]] | None = None
    warnings: dict[str, str] = {}
    for attempt in range(2):
        letter = _letter(model, letter_facts, advert, "\n".join(f"- {v}" for v in warnings.values()))
        if letter is None:
            break
        paras = {f"para{i}": p for i, p in enumerate(letter.paragraphs)}
        warnings = _check(check_model, cv_text, advert, paras)
        log.info("%s: letter draft %d, %d problems", row["id"], attempt + 1, len(warnings))
        # A rewrite can come back worse, so keep whichever draft has fewer problems.
        if best is None or len(warnings) < len(best[1]):
            best = (letter, warnings)
        if not warnings:
            break
    if best is None:
        return None
    letter, warnings = best

    out = out_root / f"{row['score']:03d}-{slug(row['company'], 25)}-{slug(row['title'])}"
    name = cv["name"].replace(" ", "-")
    render("cv", data, out / f"{name}-CV.pdf")
    render("letter", {
        "name": cv["name"],
        "contact": cv["contact"],
        "date": f"{date.today().day} {date.today():%B %Y}",
        "greeting": letter.greeting,
        "paragraphs": letter.paragraphs,
        "sign_off": "Kind regards,",
    }, out / f"{name}-Cover-Letter.pdf")
    (out / "job.json").write_text(json.dumps({
        "id": row["id"],
        "title": row["title"],
        "company": row["company"],
        "location": row["location"],
        "salary": row["salary"],
        "source": row["source"],
        "url": row["url"],
        "apply_url": row["apply_url"],
        "score": row["score"],
        "score_detail": json.loads(row["score_detail"]),
        "cv_lines_reverted": reverted,
        # Left in the letter after a rewrite; shown on the page so they get read before sending.
        "letter_warnings": warnings,
        "cover_letter": [letter.greeting, *letter.paragraphs],
    }, indent=2, ensure_ascii=False))
    return out
