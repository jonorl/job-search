# job-search

Finds remote software jobs open to the UK or the EU, scores them against my master CV and shortlists the best five. Later stages will write a tailored CV and cover letter for each and publish them to a web page.

## Pipeline

1. **Fetch** from public job APIs (Reed and Adzuna for the UK; Remotive, RemoteOK, Himalayas and Arbeitnow) and company boards (Greenhouse, Lever, Ashby). Every job is stored in SQLite, so nothing comes back twice.
2. **Filter** by title, remote, location and age (`config.yaml`). This step costs nothing.
3. **Score** what passes against `cv/master.yaml` with Gemini Flash-Lite: a score from 0 to 100, reasons, gaps, and checks for location and language.
4. **Shortlist** the top five above `min_score`.

Still to build: tailoring the CV and cover letter (rendered with Typst), the web page, and a cron job on contabo.

## Usage

```sh
python -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env   # add GEMINI_API_KEY and the job board keys

.venv/bin/job-search run     # fetch, score and print the shortlist
.venv/bin/job-search fetch   # or one step at a time
.venv/bin/job-search score --limit 10
.venv/bin/job-search top -n 5
.venv/bin/job-search stats
```

`cv/master.yaml` is the only source of facts. Tailored CVs may select, reorder and reword it, never add to it.
