# job-search

Finds remote software jobs I can do from where I am (worldwide, EMEA, or UK/EU roles that hire abroad), scores them against my master CV, and writes a tailored CV and cover letter for the best five.

## Pipeline

1. **Fetch** from public job APIs (Reed and Adzuna for the UK; Remotive, RemoteOK, Himalayas and Arbeitnow) and company boards (Greenhouse, Lever, Ashby). Every job is stored in SQLite, so nothing comes back twice.
2. **Filter** by title, remote, location and age (`config.yaml`). This step costs nothing.
3. **Score** what passes against `cv/master.yaml` with Gemini Flash-Lite: a score from 0 to 100, reasons, gaps, and checks for location and language.
4. **Tailor** the top five above `min_score`. Gemini Flash picks and rewords projects, skills and bullets from the master CV, then a second pass checks each reworded line against the line it came from and puts back the original wording if anything was added. It writes a cover letter from the checked CV and checks that too. Anything it can't fix is listed in `job.json` under `letter_warnings`, so read those before sending.
5. **Render** both documents to PDF with Typst in `output/<date>/<job>/`, in Carlito (bundled in `fonts/`, OFL) on A4.

6. **Review** on the web page (`job-search serve`). Each application has its PDFs, apply link, fit reasons and any warnings, and three buttons:
   - **Reject** tailors the next best lead in the background.
   - **Interested** answers the form's extra questions from the master CV and `cv/answers.yaml`, and flags anything that needs a fact neither gives (salary, right to work). Greenhouse questions come from its public API; for other sites, paste the questions or a plain-HTML application URL.
   - **Applied** adds a row to the Google Sheet of applications.

   Companies applied to in the last 12 months (per the sheet) are neither scored nor shortlisted.

Still to build: a cron job on contabo.

## Applications sheet

The app reads and writes the sheet with a Google service account:

1. In [Google Cloud console](https://console.cloud.google.com), create a project and enable the Google Sheets API.
2. Under IAM & Admin → Service accounts, create one (no roles needed), then Keys → Add key → JSON. Save the file outside the repo.
3. Share the sheet with the service account's email as Editor.
4. In `.env`, set `GOOGLE_SERVICE_ACCOUNT_FILE` to the key's path and `APPLICATIONS_SHEET_ID` to the id in the sheet's URL.
5. In `config.yaml`, set `applications.columns` to the sheet's header names.

## Models and Otari

Every model call goes through [Otari](https://github.com/mozilla-ai/otari), Mozilla.ai's open-source LLM gateway, when `OTARI_API_KEY` is set. The app asks for a routing policy (`job-score`, `job-tailor`, `job-check` in [otari/routing.yml](otari/routing.yml)) and Otari tries open-weight models first, falling back to Gemini, with budgets and usage tracked in its dashboard. Without an Otari key the app calls Gemini and Groq directly, using the chains in `config.yaml`.

## Usage

```sh
python -m venv .venv && .venv/bin/pip install -e .
cp .env.example .env   # add GEMINI_API_KEY and the job board keys
cp cv/master.example.yaml cv/master.yaml   # then fill in your CV
cp cv/answers.example.yaml cv/answers.yaml # stock answers for application forms

.venv/bin/job-search run     # fetch, score and print the shortlist
.venv/bin/job-search fetch   # or one step at a time
.venv/bin/job-search score --limit 10
.venv/bin/job-search top -n 5
.venv/bin/job-search tailor            # CV and letter for the top five
.venv/bin/job-search tailor --job ID   # regenerate one, e.g. after editing the master CV
.venv/bin/job-search stats
.venv/bin/job-search serve             # the review page, on http://127.0.0.1:8000
```

`cv/master.yaml` is the only source of facts. Tailored CVs may select, reorder and reword it, never add to it.
