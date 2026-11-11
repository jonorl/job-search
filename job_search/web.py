"""The review page: each tailored application, with reject / interested / applied.

Rejecting tailors the next lead in the background; interested answers the form's extra questions;
applied records the application in the Google Sheet. Bind it to localhost and put auth in front
(Caddy basic auth on the server), since it has no login of its own.
"""

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import db, llm
from .applications import Sheet, SheetError
from .cli import ROOT, next_leads, tailor_rows
from .questions import answer, load_stock_answers

log = logging.getLogger(__name__)
OUTPUT = ROOT / "output"
STATIC = Path(__file__).parent / "static"

app = FastAPI()
_paths: dict[str, Path] = {}
_lock = threading.Lock()
# Work in progress, by job id, plus "leads" for the next-lead queue; shown on the page while it runs.
_tasks: dict[str, dict] = {}
# One at a time, so two rejects in a row don't both pick the same next job.
_leads = ThreadPoolExecutor(max_workers=1)
_answers = ThreadPoolExecutor(max_workers=2)
# Past these, a task is reported as failed. The thread can't be stopped, so if it does finish later
# its result still replaces the error. Tailoring makes several more calls than answering.
LIMITS = {"leads": 15 * 60, "answer": 8 * 60}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def _outputs() -> dict[str, Path]:
    """Each job's newest output folder, by job id."""
    found = {}
    for f in sorted(OUTPUT.glob("*/*/job.json")):
        found[json.loads(f.read_text())["id"]] = f.parent
    return found


def _row(conn, job_id: str):
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "no such job")
    return row


def _set_task(key: str, **state) -> None:
    with _lock:
        if state:
            if state.get("state") == "working":
                state["started"] = time.time()
            _tasks[key] = state
        else:
            _tasks.pop(key, None)


def _task(key: str) -> dict | None:
    task = _tasks.get(key)
    limit = LIMITS["leads" if key == "leads" else "answer"]
    if task and task["state"] == "working" and time.time() - task["started"] > limit:
        return {"state": "error", "message": f"No result after {limit // 60} minutes; the models are probably "
                "overloaded or rate-limited. Try again later, or check the server log."}
    return task


def _next_lead() -> None:
    llm.reset()
    conn = db.connect()
    try:
        config, cv = _load(_paths["config"]), _load(_paths["cv"])
        rows = next_leads(conn, config, 3)
        if not rows:
            _set_task("leads", state="error", message="No more scored jobs above the threshold; run score.")
        elif tailor_rows(conn, config, cv, rows, 1):
            _set_task("leads")
        else:
            _set_task("leads", state="error", message="Couldn't tailor the next lead; see the server log.")
    except Exception as e:
        log.exception("next lead failed")
        _set_task("leads", state="error", message=str(e))
    finally:
        conn.close()


def _answer(job_id: str, url: str, pasted: str) -> None:
    llm.reset()
    conn = db.connect()
    try:
        config = _load(_paths["config"])
        stock = load_stock_answers(_paths["cv"].with_name("answers.yaml"))
        answer(_row(conn, job_id), _load(_paths["cv"]), stock, config["tailoring"], _outputs()[job_id], url, pasted)
        _set_task(job_id)
    except Exception as e:
        log.exception("answering %s failed", job_id)
        _set_task(job_id, state="error", message=str(e))
    finally:
        conn.close()


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/jobs")
def jobs():
    conn = db.connect()
    try:
        items = []
        for job_id, folder in _outputs().items():
            row = conn.execute("SELECT status, applied_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
            data = json.loads((folder / "job.json").read_text())
            app_file = folder / "application.json"
            items.append({
                **data,
                "status": row["status"] if row else "unknown",
                "applied_at": row["applied_at"] if row else None,
                "folder": folder.relative_to(OUTPUT).as_posix(),
                "files": sorted(p.name for p in folder.glob("*.pdf")),
                "application": json.loads(app_file.read_text()) if app_file.exists() else None,
                "task": _task(job_id),
            })
        items.sort(key=lambda j: (-j["score"], j["folder"]))
        target = _load(_paths["config"])["applications"].get("daily_target", 5)
        return {"jobs": items, "leads": _task("leads"), "daily_target": target}
    finally:
        conn.close()


class Action(BaseModel):
    id: str = ""
    action: str
    url: str = ""
    questions: str = ""


@app.post("/api/action")
def action(a: Action):
    if a.action == "lead":
        _set_task("leads", state="working", message="Tailoring the next lead…")
        _leads.submit(_next_lead)
        return {"ok": True}
    conn = db.connect()
    try:
        row = _row(conn, a.id)
        if a.action == "reject":
            db.set_status(conn, a.id, "rejected")
            _set_task("leads", state="working", message="Tailoring the next lead…")
            _leads.submit(_next_lead)
        elif a.action == "interested":
            db.set_status(conn, a.id, "interested")
            if a.url or a.questions or row["apply_url"]:
                _set_task(a.id, state="working", message="Answering the application questions…")
                _answers.submit(_answer, a.id, a.url, a.questions)
        elif a.action == "applied":
            config = _load(_paths["config"])["applications"]
            try:
                Sheet(config).append({
                    "date": date.today().isoformat(),
                    "company": row["company"],
                    "role": row["title"],
                    "link": row["apply_url"] or row["url"],
                    "source": row["source"],
                })
            except SheetError as e:
                # Left unchanged, so applied is only shown once the sheet has it.
                raise HTTPException(502, f"Couldn't update the spreadsheet: {e}")
            db.set_applied(conn, a.id)
        elif a.action == "undo":
            db.set_status(conn, a.id, "shortlisted")
        else:
            raise HTTPException(400, "unknown action")
        return {"ok": True}
    finally:
        conn.close()


@app.get("/files/{day}/{folder}/{name}")
def files(day: str, folder: str, name: str):
    path = (OUTPUT / day / folder / name).resolve()
    if not path.is_relative_to(OUTPUT.resolve()) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


def serve(host: str, port: int, config: Path, cv: Path) -> None:
    _paths.update(config=config, cv=cv)
    uvicorn.run(app, host=host, port=port, log_level="warning")
