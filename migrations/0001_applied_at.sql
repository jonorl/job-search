-- When each job was marked applied, for the date on the Applied tab and the daily counter.
-- UTC ISO 8601 timestamp; the page shows it in the browser's own time zone.
-- Apply by hand to every copy of jobs.db (local data/jobs.db and the server's /root/job-search/data/jobs.db)
-- before deploying the code that writes it. SQLite has no ADD COLUMN IF NOT EXISTS, so run it once:
-- a second run fails with "duplicate column name", which is harmless.

ALTER TABLE jobs ADD COLUMN applied_at TEXT;

-- ROLLBACK (SQLite 3.35+):
-- ALTER TABLE jobs DROP COLUMN applied_at;
