import argparse
import json
import logging
import os
import sqlite3
import threading
from pathlib import Path

import yaml

from . import db
from .filters import reject_reason
from .score import JobScore, score_jobs
from .sources import enrich, fetch_all

log = logging.getLogger("job_search")
ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path) -> None:
    # Cron gives a bare environment, so secrets come from .env; real env vars still win.
    if path.exists():
        for line in path.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#") and value:
                os.environ.setdefault(key.strip(), value.strip())


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text())


def cmd_fetch(conn: sqlite3.Connection, config: dict) -> None:
    jobs = fetch_all(config["sources"])
    fresh = db.insert_new(conn, jobs)
    passed = []
    for job in fresh:
        reason = reject_reason(job, config["search"])
        db.set_filtered(conn, job.id, reason)
        if reason is None:
            passed.append(job)
    for job_id, detail in enrich(passed).items():
        db.set_details(conn, job_id, **detail)
    conn.commit()
    log.info("fetched %d, new %d, passed filters %d", len(jobs), len(fresh), len(passed))


def cmd_score(conn: sqlite3.Connection, config: dict, cv: dict, limit: int | None) -> None:
    cfg = config["scoring"]
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'passed' ORDER BY posted_at DESC NULLS LAST LIMIT ?",
        (limit or cfg["max_per_run"],),
    ).fetchall()
    log.info("scoring %d jobs with %s", len(rows), cfg["model"])
    lock = threading.Lock()

    def on_scored(row, result: JobScore) -> None:
        # Hard disqualifiers override whatever number the model gave.
        eligible = result.fully_remote and result.location_eligible and result.language_ok
        score = result.score if eligible else min(result.score, 20)
        with lock:
            db.set_score(conn, row["id"], score, result.model_dump())

    score_jobs(rows, cv, cfg, config["search"]["languages"], on_scored)


def cmd_top(conn: sqlite3.Connection, config: dict, n: int | None) -> None:
    cfg = config["scoring"]
    rows = conn.execute(
        "SELECT * FROM jobs WHERE status = 'scored' AND score >= ? ORDER BY score DESC, posted_at DESC LIMIT ?",
        (cfg["min_score"], n or cfg["shortlist_size"]),
    ).fetchall()
    if not rows:
        print("Nothing scored above the threshold yet.")
    for r in rows:
        detail = json.loads(r["score_detail"])
        print(f"\n[{r['score']}] {r['title']} — {r['company']} ({r['location'] or 'n/a'}) via {r['source']}")
        print(f"  {r['apply_url']}")
        for reason in detail["reasons"]:
            print(f"  + {reason}")
        for gap in detail["gaps"]:
            print(f"  - {gap}")


def cmd_stats(conn: sqlite3.Connection) -> None:
    for r in conn.execute("SELECT status, count(*) n FROM jobs GROUP BY status ORDER BY n DESC"):
        print(f"{r['status']:>14}  {r['n']}")
    print("\nTop filter reasons:")
    for r in conn.execute(
        """SELECT CASE WHEN filter_reason LIKE 'location:%' THEN 'location' ELSE filter_reason END reason,
                  count(*) n FROM jobs WHERE filter_reason IS NOT NULL GROUP BY reason ORDER BY n DESC LIMIT 8"""
    ):
        print(f"{r['reason']:>20}  {r['n']}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="job-search")
    parser.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    parser.add_argument("--cv", type=Path, default=ROOT / "cv" / "master.yaml")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="fetch new jobs and apply the rule filters")
    p = sub.add_parser("score", help="score jobs that passed the filters")
    p.add_argument("--limit", type=int)
    p = sub.add_parser("top", help="print the shortlist")
    p.add_argument("-n", type=int)
    sub.add_parser("stats", help="counts by status")
    sub.add_parser("run", help="fetch, score and print the shortlist")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv(ROOT / ".env")
    config = load_yaml(args.config)
    conn = db.connect()

    if args.command in ("fetch", "run"):
        cmd_fetch(conn, config)
    if args.command in ("score", "run"):
        cmd_score(conn, config, load_yaml(args.cv), getattr(args, "limit", None))
    if args.command in ("top", "run"):
        cmd_top(conn, config, getattr(args, "n", None))
    if args.command == "stats":
        cmd_stats(conn)


if __name__ == "__main__":
    main()
