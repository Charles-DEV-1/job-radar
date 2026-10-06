# Job Radar

Private, single-user job monitoring for Nigerian and African software engineering roles. Stage 1 implements company configuration and public ATS detection. Project rules are in [AGENTS.md](AGENTS.md).

## Setup

Install Python 3.12, then run from the repository root (PowerShell):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "./pipeline[dev]"
Copy-Item .env.example .env
```

Stage 1 needs no API keys or `.env` values. Keep private resumes under `data/resumes/`; only nonprivate templates belong in `data/resumes/templates/`.

## Detect ATS boards

Replace the three disabled, fictional examples in `data/companies.yaml` with your company entries, official domains, and `enabled: true`. `careers_url` is optional. Run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pipeline.detect_ats
.\.venv\Scripts\python.exe -m pipeline.detect_ats --company "Company Name"
.\.venv\Scripts\python.exe -m pipeline.detect_ats --write
```

The installed package also supports `python -m pipeline.detect_ats` in an activated environment. Use `--companies-file PATH` for another list and `--report-file PATH` for another run-log location.

Official links are checked first, then up to six name/alias/domain candidates are tried until a confirmed board is found. Discovery reads at most four official pages, checks robots rules before each page or redirect, and honors longer crawl delays. Requests use an honest User-Agent, a 10-second timeout, up to two retries with backoff, and at most one request/second per host.

`--write` updates only `ats`, `ats_slug`, and `detection_status`, preserving YAML comments and ordering. Existing empty boards remain valid. Slug-only matches need review; conflicting company identities also need review. `none/not_found` means the bounded search completed without finding a board; incomplete discovery becomes `unknown/needs_review`. This is discovery, not an exhaustive search or a guarantee that a board is the company's current primary ATS.

Every probe and company result is recorded in gitignored `data/detection-runs.jsonl`, with status, count, error, duration, and ownership evidence. Descriptions are excluded. Exit codes: `0` completed, `1` completed with source/page errors, `2` invalid input or file failure. Health alerts come in a later stage.

## Checks

```powershell
Set-Location pipeline
..\.venv\Scripts\python.exe -m pytest
..\.venv\Scripts\python.exe -m ruff check .
..\.venv\Scripts\python.exe -m ruff format --check .
```

`pipeline/src/pipeline/` contains the CLI and six isolated adapters. `fetch_jobs()` returns unchanged provider JSON objects; SmartRecruiters pagination is supported. Tests mock all HTTP. Live endpoint fixtures and five-company results are documented in [the fixture notes](pipeline/tests/fixtures/README.md).

`dashboard/`, `supabase/migrations/`, and `.github/workflows/` remain reserved for later stages.
