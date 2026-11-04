"""Answers an application form's extra questions from the master CV and the candidate's stock answers.

Questions come from Greenhouse's public API when the job is on Greenhouse, from the page itself when it
serves plain HTML, and otherwise from text pasted in; many job pages refuse scripts, and that is respected.
"""

import json
import logging
import re
from pathlib import Path

import httpx
import yaml
from pydantic import BaseModel, Field

from .llm import generate
from .sources import strip_html
from .tailor import CHECK_SYSTEM, EXPERIENCE, _advert, _master_with_ids, _report

log = logging.getLogger(__name__)

GREENHOUSE = re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?([\w-]+)/jobs/(\d+)|[?&]gh_jid=(\d+)")


class Answer(BaseModel):
    question: str = Field(description="The question as the form asks it")
    answer: str = Field(description="What to put in the field; empty if needs_input")
    needs_input: bool = Field(description="True if answering needs a fact neither the CV nor the stock answers give")
    note: str = Field(description="For needs_input, what is missing; otherwise empty")


class Answers(BaseModel):
    answers: list[Answer]


SYSTEM = f"""You fill in the extra questions on a job application form for a candidate, in British \
English. You get the candidate's master CV, their stock answers, the job advert, the cover letter \
already written, and the form's questions (or the raw text of the form page, from which you pick \
out the questions yourself).

Facts come only from the CV and the stock answers. Never invent a salary, notice period, date, right \
to work, location, number or experience. If a question needs a fact neither gives, set needs_input \
and say what is missing. Skip fields the candidate fills in by hand anyway: name, email, phone, CV \
and cover-letter uploads.

{EXPERIENCE}

Free-text answers: specific, plain and short (usually 2-4 sentences, or within any stated limit), \
drawing on the CV lines most relevant to the advert. Multiple choice: answer with one of the options \
exactly as written."""


def greenhouse_questions(url: str) -> str | None:
    m = GREENHOUSE.search(url)
    if not m or not m.group(1):
        return None
    r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}",
                  params={"questions": "true"}, timeout=30)
    r.raise_for_status()
    lines = []
    for q in r.json().get("questions", []):
        options = [v["label"] for f in q.get("fields", []) for v in f.get("values", [])]
        kinds = {f["type"] for f in q.get("fields", [])}
        if kinds <= {"input_file"}:
            continue
        line = f"- {q['label']}{' (required)' if q.get('required') else ''}"
        if options:
            line += f" [options: {' | '.join(options)}]"
        lines.append(line)
    return "\n".join(lines)


def page_text(url: str) -> str:
    """The text of a plain-HTML application page; ValueError if it can't be read."""
    r = httpx.get(url, follow_redirects=True, timeout=30)
    if r.status_code != 200:
        raise ValueError(f"the page answered {r.status_code}; open it and paste the questions instead")
    # Form labels and options matter more than layout, so keep the text and drop the markup.
    text = strip_html(re.sub(r"<(script|style|svg)\b.*?</\1>", "", r.text, flags=re.S | re.I))
    if len(text) < 200:
        raise ValueError("the page is built by JavaScript, so there's nothing to read; paste the questions instead")
    return text[:30000]


def load_stock_answers(path: Path) -> dict:
    return yaml.safe_load(path.read_text()) if path.exists() else {}


def answer(row, cv: dict, stock: dict, cfg: dict, out: Path, url: str = "", pasted: str = "") -> dict:
    """Writes application.json beside the job's CV and returns it."""
    source, target = pasted.strip(), url or row["apply_url"] or row["url"]
    if not source:
        source = greenhouse_questions(target) or page_text(target)
    letter = json.loads((out / "job.json").read_text()).get("cover_letter", [])
    prompt = (
        f"<master_cv>\n{_master_with_ids(cv)}\n</master_cv>\n\n"
        f"<stock_answers>\n{yaml.safe_dump(stock, allow_unicode=True)}</stock_answers>\n\n"
        f"{_advert(row)}\n\n<cover_letter>\n{chr(10).join(letter)}\n</cover_letter>\n\n"
        f"<form>\n{source}\n</form>"
    )
    result = generate(cfg["models"], SYSTEM, prompt, Answers, thinking="MEDIUM")
    if result is None:
        raise ValueError("no model could answer the questions")
    answers = [a.model_dump() for a in result.answers]

    # Free-text answers make claims, so they get the same check as the letter.
    claims = {f"a{i}": f"{a['question']}: {a['answer']}" for i, a in enumerate(answers)
              if a["answer"] and len(a["answer"]) > 60}
    if claims:
        cv_text = _master_with_ids(cv) + "\n\nstock answers:\n" + yaml.safe_dump(stock, allow_unicode=True)
        prompt = f"<master_cv>\n{cv_text}\n</master_cv>\n\n{_advert(row)}\n\n<lines>\n" + \
            "\n".join(f"[{k}] {v}" for k, v in claims.items()) + "\n</lines>"
        for key, issue in _report(cfg["check_models"], CHECK_SYSTEM, prompt, claims).items():
            answers[int(key[1:])]["warning"] = issue

    data = {"source": "pasted" if pasted.strip() else target, "answers": answers}
    (out / "application.json").write_text(json.dumps(data, indent=2, ensure_ascii=False))
    return data
