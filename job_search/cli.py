import argparse
import json
import logging
import os
import sqlite3
import threading
from datetime import date
from pathlib import Path

import yaml

from . import db
from .applications import company_key, recent_companies
from .filters import reject_reason
from .llm import QuotaExhausted
from .score import JobScore, score_jobs
from .sources import enrich, fetch_all
from .tailor import tailor

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


def cmd_score(conn: sqlite3.Connection, config: dict, cv: dict, limit: int | None, rescore: bool) -> None:
    cfg = config["scoring"]
    # Rescoring is for after the CV or the scoring rules change; shortlisted and applied jobs are left alone.
    statuses = ("passed", "scored") if rescore else ("passed",)
    rows = conn.execute(
        f"""SELECT * FROM jobs WHERE status IN ({",".join("?" * len(statuses))})
            ORDER BY posted_at DESC NULLS LAST LIMIT ?""",
        (*statuses, limit or cfg["max_per_run"]),
    ).fetchall()
    # Scoring a company already applied to would only spend quota on a job that can't be shortlisted.
    recent = recent_companies(config.get("applications"))
    for row in [r for r in rows if company_key(r["company"]) in recent]:
        db.set_filtered(conn, row["id"], "applied in the last 12 months")
        rows.remove(row)
    conn.commit()
    log.info("scoring %d jobs with %s", len(rows), cfg["models"][0])
    lock = threading.Lock()

    def on_scored(row, result: JobScore) -> None:
        # Hard disqualifiers override whatever number the model gave.
        eligible = result.fully_remote and result.location_eligible and result.language_ok
        score = result.score if eligible else min(result.score, 20)
        with lock:
            db.set_score(conn, row["id"], score, result.model_dump())

    try:
        score_jobs(rows, cv, cfg, config["search"], on_scored)
    except QuotaExhausted as e:
        log.warning("every scoring model is out of quota for today (%s); the rest stay queued", e)


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


def next_leads(conn: sqlite3.Connection, config: dict, n: int) -> list[sqlite3.Row]:
    """The best scored jobs, one per company, skipping companies already in play or recently applied to."""
    taken = recent_companies(config.get("applications"))
    taken |= {company_key(r["company"]) for r in conn.execute(
        "SELECT company FROM jobs WHERE status IN ('shortlisted', 'interested', 'applied')")}
    # One job per company, so a firm with several similar adverts can't take the whole shortlist.
    rows = []
    for row in conn.execute(
        "SELECT * FROM jobs WHERE status = 'scored' AND score >= ? ORDER BY score DESC, posted_at DESC",
        (config["scoring"]["min_score"],),
    ):
        if company_key(row["company"]) not in taken:
            taken.add(company_key(row["company"]))
            rows.append(row)
        if len(rows) == n:
            break
    return rows


def tailor_rows(conn: sqlite3.Connection, config: dict, cv: dict, rows, wanted: int) -> int:
    """Tailors rows in order until `wanted` succeed; returns how many did."""
    out_root = ROOT / "output" / date.today().isoformat()
    done = 0
    for row in rows:
        if done == wanted:
            break
        log.info("tailoring %s: %s at %s", row["id"], row["title"], row["company"])
        try:
            out = tailor(row, cv, config["tailoring"], out_root)
        except QuotaExhausted as e:
            log.warning("every tailoring model is out of quota for today (%s); the rest stay queued", e)
            break
        if out is None:
            log.warning("could not tailor %s; it stays in the queue", row["id"])
            continue
        db.set_status(conn, row["id"], "shortlisted")
        log.info("wrote %s", out.relative_to(ROOT))
        done += 1
    return done


def cmd_tailor(conn: sqlite3.Connection, config: dict, cv: dict, n: int | None, job_id: str | None) -> None:
    n = n or config["scoring"]["shortlist_size"]
    if job_id:
        # Regenerates one job whatever its status, e.g. after editing the master CV or the prompts.
        rows = conn.execute("SELECT * FROM jobs WHERE id = ? AND score IS NOT NULL", (job_id,)).fetchall()
        tailor_rows(conn, config, cv, rows, 1)
    else:
        # Candidates beyond n stand in for any that fail to tailor.
        tailor_rows(conn, config, cv, next_leads(conn, config, n * 2), n)


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
    p.add_argument("--rescore", action="store_true", help="also re-score jobs already scored")
    p = sub.add_parser("top", help="print the shortlist")
    p.add_argument("-n", type=int)
    p = sub.add_parser("tailor", help="write a CV and cover letter for each of the top jobs")
    p.add_argument("-n", type=int)
    p.add_argument("--job", help="regenerate this job id instead")
    sub.add_parser("stats", help="counts by status")
    sub.add_parser("run", help="fetch, score and tailor the shortlist")
    p = sub.add_parser("serve", help="the review page")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("google_genai").setLevel(logging.WARNING)
    load_dotenv(ROOT / ".env")
    config = load_yaml(args.config)
    conn = db.connect()

    if args.command in ("fetch", "run"):
        cmd_fetch(conn, config)
    if args.command in ("score", "run"):
        cmd_score(conn, config, load_yaml(args.cv), getattr(args, "limit", None), getattr(args, "rescore", False))
    if args.command == "top":
        cmd_top(conn, config, args.n)
    if args.command in ("tailor", "run"):
        cmd_tailor(conn, config, load_yaml(args.cv), getattr(args, "n", None), getattr(args, "job", None))
    if args.command == "stats":
        cmd_stats(conn)
    if args.command == "serve":
        from .web import serve

        serve(args.host, args.port, args.config, args.cv)


if __name__ == "__main__":
    main()
