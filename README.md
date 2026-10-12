# Job Radar

Private, single-user job monitoring for Nigerian and African software engineering roles. Stages 1–3 cover ATS discovery, fetching, careers-page monitoring, verified job feeds, filtering, dedupe, and private Supabase storage. Project rules are in [AGENTS.md](AGENTS.md).

## Setup

Install Python 3.12, then run from the repository root (PowerShell):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "./pipeline[dev]"
Copy-Item .env.example .env
```

Stage 1 needs no API keys. Stage 2 reads `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` from environment variables or `.env`. Keep private resumes under `data/resumes/`; only nonprivate templates belong in `data/resumes/templates/`.

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

To include the real PostgreSQL integration checks, install Node.js and the pinned test engine once from the repository root:

```powershell
npm.cmd install --prefix .tools/pglite --cache .tools/npm-cache --no-audit --no-fund @electric-sql/pglite@0.5.8
```

The dependency version is also recorded in `pipeline/tests/db_engine/package.json`. SQL integration checks are explicitly skipped if Node/PGlite is unavailable; the remaining tests still run.

```powershell
Set-Location pipeline
..\.venv\Scripts\python.exe -m pytest
..\.venv\Scripts\python.exe -m ruff check .
..\.venv\Scripts\python.exe -m ruff format --check .
```

## Fetch and store

Apply [the Supabase migration](supabase/README.md), configure your service key, and enable your real companies with confirmed ATS/slug fields. Edit role and location lists in `data/filters.yaml` as needed.

```powershell
.\.venv\Scripts\python.exe -m pipeline.run_fetch --concurrency 4
.\.venv\Scripts\python.exe -m pipeline.run_fetch --concurrency 4
```

The first successful fetch backfills existing jobs with notifications suppressed. An unchanged second run reports zero new jobs. The summary distinguishes stored, new notifiable, edited, duplicate, and closed jobs. Run records go to Supabase and gitignored `data/fetch-runs.jsonl`. Exit codes: `0` successful, `1` source errors, `2` setup/configuration failure. Full snapshots commit atomically; one failing source does not stop the others. Nothing is emailed or tailored in this stage.

`pipeline/src/pipeline/` contains the CLIs and six isolated adapters. Fetches return complete provider JSON, including verified description requests and SmartRecruiters pagination/details. Tests mock all external HTTP using recorded ATS fixtures; storage semantics execute against local PostgreSQL. [Fixture notes](pipeline/tests/fixtures/README.md) list verified endpoints and real-job filter samples.

`dashboard/` and `.github/workflows/` remain reserved for later stages.

## Stage 3 careers pages and supplied feeds

Apply the [Stage 3 forward migration](supabase/migrations/20261010000100_stage3_careers_monitor.sql) after Stage 2. Install the extraction SDKs and Chromium:

```powershell
.\.venv\Scripts\python.exe -m pip install -e "./pipeline[dev,llm]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

For this existing uv-managed environment without pip, use `.tools\uv\uv.exe pip install --python .venv\Scripts\python.exe --cache-dir .tools\cache -e "./pipeline[dev,llm]"`. If Chromium's download is unavailable, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to an installed Chromium-based browser executable. It runs headlessly in an isolated process.

Configure `LLM_PROVIDER` (`openai` or `anthropic`), `LLM_API_KEY`, and `LLM_MODEL`. Missing configuration causes a recorded extraction error on changed pages; ATS and feed sources continue. Unchanged pages need no LLM credentials or calls. Optional `LLM_INPUT_USD_PER_MILLION` and `LLM_OUTPUT_USD_PER_MILLION` enable a cost estimate without guessing model prices. Each run records attempts, known tokens, cost (null if unknown), and uncounted attempts.

Enabled companies with a `careers_url` and `detection_status: not_found` or `ats: unknown` automatically get a `careers_page` source. Confirmed ATS sources keep their existing adapter. Run the same `pipeline.run_fetch` command; `--feeds-file PATH` defaults to [data/feeds.yaml](data/feeds.yaml).

Plain HTTP runs first. Under 100 characters of cleaned text or a loading/JavaScript shell triggers Chromium, except explicit no-vacancies messages. The successful method is cached in private Supabase state. Every page has a 45-second wall timeout, bounded redirects/retries, an 8 MB response cap, robots checks on each target, and the shared host limiter (at least one second, or a longer robots crawl delay). Browser GETs, including scripts and redirects, are fulfilled through that same client; non-GET requests, service workers, sockets, images, media, and fonts are blocked.

Snapshots remove navigation, footer, hidden content, cookies, banners, carousels, timestamps, dates, and known tracking parameters. Listing reordering does not change the hash. Date-shaped job URL identifiers remain intact. A matching hash skips extraction. A change sends only added/replaced regions with two nearby text lines and local link context; a first successful run uses the cleaned page as backfill. Changed inputs over 60,000 characters fail visibly and keep the previous baseline.

The LLM returns a strict JSON array of `{title, location, apply_url, description_snippet}`; nullable fields are required and extra fields are rejected. Apply URLs must be absolute, appear in actual page links, and use the company's registrable domain or one of the six existing ATS domains. Fabricated URLs, empty titles, and redirects outside those domains are rejected. Detail descriptions are fetched when permitted; failures retain the extracted snippet and record a warning. Each canonical job uses `source_type: careers_page` and a SHA-256 of its cleaned apply URL as `external_id`.

Empty/failed pages, unfinished shells, and snapshots below 40% of a previous snapshot of at least 200 characters record an error. A failed extraction does not advance the hash. Jobs, snapshot, method, usage, and warnings commit atomically; stale concurrent baselines are rejected. **Page/feed diffs do not infer job closure:** previous jobs are retained because a partial region or rolling feed cannot prove a vacancy disappeared. This assumption preserves Stage 2 status, dedupe, backfill, and notification suppression.

`data/feeds.yaml` is empty until you supply verified Nigeria/Africa job-only URLs. Each entry requires `name`, `company`, `domain`, `url`, `enabled`, `verified`, and `scope` (`nigeria` or `africa`). Enable only after checking a real response. The adapter validates every response before storage: RSS/Atom or JSON with a root array or `jobs`/`items` array, using `title`, `apply_url`/`url`, nullable `location`, and `description_text`/`content_text`/`content_html`/`description`. Unexpected HTML, malformed XML/JSON/items, and a previously nonempty feed returning zero are errors. No feed URLs are guessed.

On 2026-10-10, TechCabal's jobs page embedded Airtable; its advertised feed and Techpoint Africa's advertised feed returned general news, not job-only postings. No job-specific RSS/JSON feed was verified for either, so both remain disabled. [Recorded verification](pipeline/tests/fixtures/stage3-verification.json) includes the real responses checked. Documented LLM endpoints returned 401 without credentials; authenticated generation remains to be verified with your configuration. The owner reported applying the Stage 3 migration on 2026-10-12; hosted database state has not been independently rechecked here.

Tests include controlled changes built from recorded job facts, recorded public HTML, mock Playwright, real PostgreSQL transactions/RLS, and optional actual Chromium rendering over mocked HTTP. To include the browser check, set `RUN_CHROMIUM_TESTS=1` before running pytest with an installed browser. Email, resumes, health-alert emails, and dashboard work belong to later stages.
