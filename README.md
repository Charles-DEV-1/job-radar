# Job Radar

Private, single-user job monitoring for Nigerian and African software engineering roles. Stages 1–4 cover discovery, fetching, careers monitoring, filtering, private storage and validated resume tailoring. Project rules are in [AGENTS.md](AGENTS.md).

## Setup

Install Python 3.12, then run from the repository root (PowerShell):

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e "./pipeline[dev]"
Copy-Item .env.example .env
```

Stage 1 and offline resume setup need no API keys. Stage 2 reads `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` from environment variables or `.env`. Real master resumes, extracted JSON, templates and output documents are gitignored. See [Stage 4 resume setup](data/RESUMES.md).

## Detect ATS boards

Replace the three disabled, fictional examples in `data/companies.yaml` with your company entries, official domains, and `enabled: true`. `careers_url` is optional. Run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pipeline.detect_ats
.\.venv\Scripts\python.exe -m pipeline.detect_ats --company "Company Name"
.\.venv\Scripts\python.exe -m pipeline.detect_ats --write
```

The installed package also supports `python -m pipeline.detect_ats` in an activated environment. Use `--companies-file PATH` for another list and `--report-file PATH` for another run-log location.

Official and explicitly configured ATS links are checked first. Every ATS provider is probed; up to six name/alias/domain candidates are tried per provider, stopping that provider's slug search only after finding a confirmed board with jobs. Confirmed boards with open jobs rank above confirmed empty boards. Discovery reads at most four permitted careers/home pages, checks robots rules before each page or redirect, and honors longer crawl delays. Requests use an honest User-Agent, a 10-second timeout, up to two retries with backoff, and at most one request/second per host.

`--write` updates only `ats`, `ats_slug`, and `detection_status`, preserving YAML comments and ordering. An empty board requires a matching company name or domain to be confirmed and is flagged `empty, verify` in the report and run log. Empty boards with missing or conflicting company identity are `not_found`, even when linked. Nonempty slug-only matches and conflicting identities need review. Parenthetical annotations are removed before comparing full names against the company and its aliases, allowing `PiggyTech Global Limited (PiggyVest)` to match an approved legal-name alias.

An explicitly supplied `careers_url` on `breezy.hr`, `workable.com`, `greenhouse.io`, `lever.co`, `ashbyhq.com`, `recruitee.com`, or `smartrecruiters.com` is allowed within its configured host and board path. Unknown external hosts and lookalike domains remain rejected. Breezy is a robots-aware careers-page source; no Breezy API endpoint is guessed. `none/not_found` means the bounded search completed without finding a supported board; incomplete discovery becomes `unknown/needs_review`. Discovery does not guarantee which board is the company's current primary ATS.

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

If `run_fetch` reports `radar_sync_sources` error `23514` with careers/feed sources, verify that this migration was applied to the same project as `SUPABASE_URL`. Stage 2's sync function treats every source as an ATS and rejects `careers_page`/`feed`. Stage 3 adds `sources.company_domain`, `source_runs.metadata`, `jobs.source_type`, and `source_monitor_state`, and replaces the sync function. Apply it through that project's SQL editor or your migration workflow; do not remove the ATS check constraint.

After Stage 3, apply [the source-sync fix](supabase/migrations/20261012000100_stage3_sync_safe_updates.sql). It makes the registry update compatible with Supabase's safe-update guard (`21000: UPDATE requires a WHERE clause`) by disabling only currently enabled sources before synchronizing the supplied list. Existing IDs, snapshots, grants, and source-type constraints are preserved. Apply this forward migration even if the original Stage 3 migration has already run.

```powershell
.\.venv\Scripts\python.exe -m pip install -e "./pipeline[dev,llm]"
.\.venv\Scripts\python.exe -m playwright install chromium
```

For this existing uv-managed environment without pip, use `.tools\uv\uv.exe pip install --python .venv\Scripts\python.exe --cache-dir .tools\cache -e "./pipeline[dev,llm]"`. If Chromium's download is unavailable, set `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to an installed Chromium-based browser executable. It runs headlessly in an isolated process.

Configure `LLM_PROVIDER` (`openai`, `anthropic`, or `groq`), `LLM_API_KEY`, and `LLM_MODEL`. Missing configuration causes a recorded extraction error on changed pages; ATS and feed sources continue. Unchanged pages need no LLM credentials or calls. Optional `LLM_INPUT_USD_PER_MILLION` and `LLM_OUTPUT_USD_PER_MILLION` enable a cost estimate without guessing model prices. Each run records attempts, known tokens, cost (null if unknown), and uncounted attempts.

Groq uses the existing OpenAI SDK's **Chat Completions** endpoint at `https://api.groq.com/openai/v1/chat/completions`, as documented in [Groq's OpenAI compatibility guide](https://console.groq.com/docs/openai) and [Chat API reference](https://console.groq.com/docs/api-reference). No base-URL variable or additional SDK is needed. Example configuration:

```dotenv
LLM_PROVIDER=groq
LLM_API_KEY=your_groq_key
LLM_MODEL=openai/gpt-oss-20b
LLM_MAX_TOKENS_PER_MINUTE=6000
```

The provider/model pairing is configurable; check your account's model permissions and [current Groq models](https://console.groq.com/docs/models). Provider support is tested with mocked HTTP for `openai/gpt-oss-20b` and `openai/gpt-oss-120b`; authenticated generation has not been tested. The Pydantic JSON schemas, URL checks, and resume truthfulness validators remain unchanged.

All provider SDK calls share a thread-safe token budget within the run's HTTP session, including concurrent page sources and retries. `LLM_MAX_TOKENS_PER_MINUTE` defaults to 6000 and must be a positive integer. A rolling 60-second window reserves estimated prompt tokens (UTF-8 bytes/3 plus 256 tokens of framing headroom) and the full allowed completion, then reconciles against actual returned usage. The completion cap is at most 4096 and is reduced when necessary to fit the configured budget; a prompt exceeding that budget fails visibly without truncating source facts. Missing usage or transport failures retain the reservation. Estimates are not exact model tokenization; separate processes and other applications sharing the organization quota need their own coordination.

HTTP 429 has at most **three retries** (four HTTP attempts). Retries wait for the greater of exponential backoff (1, 2, 4 seconds) and `Retry-After`, including fractional seconds or an HTTP date, without shortening the server's delay. SDK automatic retries are disabled; exhausted rate limits stop outer JSON-validation retries as well. [Groq's rate-limit documentation](https://console.groq.com/docs/rate-limits) describes the server headers and organization-level quotas. An exhausted page extraction records an error and retains the previous baseline so the same change is retried on the next run. Resume tailoring instead follows its existing failed-status/master-file fallback path. Retry attempts are recorded; their unavailable usage makes the cost estimate null.

Enabled companies with a `careers_url` and `detection_status: not_found` or `ats: unknown` automatically get a `careers_page` source. Confirmed ATS sources keep their existing adapter. Run the same `pipeline.run_fetch` command; `--feeds-file PATH` defaults to [data/feeds.yaml](data/feeds.yaml).

Plain HTTP runs first. Under 100 characters of cleaned text or a loading/JavaScript shell triggers Chromium, except explicit no-vacancies messages. The successful method is cached in private Supabase state. Every page has a 45-second wall timeout, bounded redirects/retries, an 8 MB response cap, robots checks on each target, and the shared host limiter (at least one second, or a longer robots crawl delay). Browser GETs, including scripts and redirects, are fulfilled through that same client; non-GET requests, service workers, sockets, images, media, and fonts are blocked.

Snapshots remove navigation, footer, hidden content, cookies, banners, carousels, timestamps, dates, and known tracking parameters. Listing reordering does not change the hash. Date-shaped job URL identifiers remain intact. A matching hash skips extraction. A change sends only added/replaced regions with two nearby text lines and local link context; a first successful run uses the cleaned page as backfill. Changed inputs over 60,000 characters fail visibly and keep the previous baseline.

The LLM returns a strict JSON array of `{title, location, apply_url, description_snippet}`; nullable fields are required and extra fields are rejected. Apply URLs must be absolute, appear in actual page links, and use the company's registrable domain or one of the known ATS domains listed above, including Breezy. Fabricated URLs, empty titles, and redirects outside those domains are rejected. Detail descriptions are fetched when permitted; failures retain the extracted snippet and record a warning. Each canonical job uses `source_type: careers_page` and a SHA-256 of its cleaned apply URL as `external_id`.

Empty/failed pages, unfinished shells, and snapshots below 40% of a previous snapshot of at least 200 characters record an error. A failed extraction does not advance the hash. Jobs, snapshot, method, usage, and warnings commit atomically; stale concurrent baselines are rejected. **Page/feed diffs do not infer job closure:** previous jobs are retained because a partial region or rolling feed cannot prove a vacancy disappeared. This assumption preserves Stage 2 status, dedupe, backfill, and notification suppression.

`data/feeds.yaml` is empty until you supply verified Nigeria/Africa job-only URLs. Each entry requires `name`, `company`, `domain`, `url`, `enabled`, `verified`, and `scope` (`nigeria` or `africa`). Enable only after checking a real response. The adapter validates every response before storage: RSS/Atom or JSON with a root array or `jobs`/`items` array, using `title`, `apply_url`/`url`, nullable `location`, and `description_text`/`content_text`/`content_html`/`description`. Unexpected HTML, malformed XML/JSON/items, and a previously nonempty feed returning zero are errors. No feed URLs are guessed.

On 2026-10-10, TechCabal's jobs page embedded Airtable; its advertised feed and Techpoint Africa's advertised feed returned general news, not job-only postings. No job-specific RSS/JSON feed was verified for either, so both remain disabled. [Recorded verification](pipeline/tests/fixtures/stage3-verification.json) includes the real responses checked. Documented LLM endpoints returned 401 without credentials; authenticated generation remains to be verified with your configuration. The owner reported applying the Stage 3 migration on 2026-10-12; hosted database state has not been independently rechecked here.

Tests include controlled changes built from recorded job facts, recorded public HTML, mock Playwright, real PostgreSQL transactions/RLS, and optional actual Chromium rendering over mocked HTTP. To include the browser check, set `RUN_CHROMIUM_TESTS=1` before running pytest with an installed browser. Resume setup/tailoring is covered in [Stage 4](data/RESUMES.md); email, health-alert emails, and dashboard work belong to later stages.
