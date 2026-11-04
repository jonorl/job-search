# job-search

Finds remote software jobs I can do from where I am (worldwide, EMEA, or UK/EU roles that hire abroad), scores them against my master CV, and writes a tailored CV and cover letter for the best five.

## Pipeline

1. **Fetch** from public job APIs (Reed and Adzuna for the UK; Remotive, RemoteOK, Himalayas and Arbeitnow) and company boards (Greenhouse, Lever, Ashby). Every job is stored in SQLite, so nothing comes back twice.
2. **Filter** by title, remote, location and age (`config.yaml`). This step costs nothing.
3. **Score** what passes against `cv/master.yaml` with Gemini Flash-Lite: a score from 0 to 100, reasons, gaps, and checks for location and language.
4. **Tailor** the top five above `min_score`. Gemini Flash picks and rewords projects, skills and bullets from the master CV, then a second pass checks each reworded line against the line it came from and puts back the original wording if anything was added. It writes a cover letter from the checked CV and checks that too. Anything it can't fix is listed in `job.json` under `letter_warnings`, so read those before sending.
5. **Render** both documents to PDF with Typst in `output/<date>/<job>/`, in Carlito (bundled in `fonts/`, OFL) on A4.

Still to build: the web page, and a cron job on contabo.

## Usage

```sh
python -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env   # add GEMINI_API_KEY and the job board keys
cp cv/master.example.yaml cv/master.yaml   # then fill in your CV

.venv/bin/job-search run     # fetch, score and print the shortlist
.venv/bin/job-search fetch   # or one step at a time
.venv/bin/job-search score --limit 10
.venv/bin/job-search top -n 5
.venv/bin/job-search tailor            # CV and letter for the top five
.venv/bin/job-search tailor --job ID   # regenerate one, e.g. after editing the master CV
.venv/bin/job-search stats
```

`cv/master.yaml` is the only source of facts. Tailored CVs may select, reorder and reword it, never add to it.
