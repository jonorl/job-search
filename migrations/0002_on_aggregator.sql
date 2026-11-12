-- When a company-board job (Greenhouse, Lever, Ashby, ...) was first seen listed on an aggregator too
-- (Reed, Adzuna, Himalayas, ...). NULL on a company-board job means direct-only so far: those are scored
-- first and ranked higher as leads. UTC ISO 8601 timestamp; the next fetch fills it in for existing rows.
-- Apply by hand to every copy of jobs.db (local data/jobs.db and the server's /root/job-search/data/jobs.db)
-- before deploying the code that reads it. Run once: a second run fails with "duplicate column name".

ALTER TABLE jobs ADD COLUMN on_aggregator TEXT;

-- ROLLBACK (SQLite 3.35+):
-- ALTER TABLE jobs DROP COLUMN on_aggregator;
