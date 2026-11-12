import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    dedupe_key TEXT NOT NULL UNIQUE,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    remote INTEGER,
    url TEXT NOT NULL,
    apply_url TEXT,
    salary TEXT,
    description TEXT,
    posted_at TEXT,
    fetched_at TEXT NOT NULL,
    -- new -> filtered_out | passed -> scored -> shortlisted -> interested | applied | rejected | expired
    status TEXT NOT NULL DEFAULT 'new',
    filter_reason TEXT,
    score INTEGER,
    score_detail TEXT,
    scored_at TEXT,
    -- Added by migrations/0001_applied_at.sql on existing databases.
    applied_at TEXT,
    -- Added by migrations/0002_on_aggregator.sql on existing databases.
    on_aggregator TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status);
"""


def connect() -> sqlite3.Connection:
    path = Path(os.environ.get("JOB_SEARCH_DB") or Path(__file__).resolve().parent.parent / "data" / "jobs.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Scoring writes from worker threads, serialised by a lock in the caller.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def insert_new(conn: sqlite3.Connection, jobs: list[Job]) -> list[Job]:
    """Store jobs not seen before and return only those."""
    now = datetime.now(UTC).isoformat()
    fresh = []
    for j in jobs:
        cur = conn.execute(
            """INSERT OR IGNORE INTO jobs
               (id, dedupe_key, source, title, company, location, remote, url, apply_url,
                salary, description, posted_at, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (j.id, j.dedupe_key, j.source, j.title, j.company, j.location,
             None if j.remote is None else int(j.remote), j.url, j.apply_url or j.url,
             j.salary, j.description, j.posted_at.isoformat() if j.posted_at else None, now),
        )
        if cur.rowcount:
            fresh.append(j)
    conn.commit()
    return fresh


def set_filtered(conn: sqlite3.Connection, job_id: str, reason: str | None) -> None:
    conn.execute(
        "UPDATE jobs SET status = ?, filter_reason = ? WHERE id = ?",
        ("filtered_out" if reason else "passed", reason, job_id),
    )


def set_details(conn: sqlite3.Connection, job_id: str, description: str, apply_url: str | None) -> None:
    conn.execute(
        "UPDATE jobs SET description = ?, apply_url = coalesce(?, apply_url) WHERE id = ?",
        (description, apply_url, job_id),
    )


def set_score(conn: sqlite3.Connection, job_id: str, score: int, detail: dict) -> None:
    conn.execute(
        "UPDATE jobs SET status = 'scored', score = ?, score_detail = ?, scored_at = ? WHERE id = ?",
        (score, json.dumps(detail), datetime.now(UTC).isoformat(), job_id),
    )
    conn.commit()


def set_applied(conn: sqlite3.Connection, job_id: str) -> None:
    conn.execute("UPDATE jobs SET status = 'applied', applied_at = ? WHERE id = ?",
                 (datetime.now(UTC).isoformat(), job_id))
    conn.commit()


def set_status(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    conn.execute("UPDATE jobs SET status = ? WHERE id = ?", (status, job_id))
    conn.commit()
