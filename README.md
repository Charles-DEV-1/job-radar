# Job Radar

Private, single-user job monitoring for Nigerian and African software engineering roles. Stages 1–5 cover discovery, fetching, careers monitoring, filtering, private storage, validated resume tailoring and the private dashboard. Project rules are in [AGENTS.md](AGENTS.md).

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

`--write` updates only `ats`, `ats_slug`, `detection_status`, and `ats_job_count`, preserving YAML comments and ordering. An empty board matched only by name is `needs_review` with reason `empty, name match only`; a domain match alone also requires review. Only a successful empty API response whose board link came from the company's own site may be confirmed and flagged `empty, verify`. A configured external ATS URL alone is insufficient for empty-board confirmation. Anonymous guessed empty responses and conflicting empty identities remain `not_found`; 404/410 responses are never promoted by a link. Parenthetical annotations are removed before comparing full names against the company and its aliases, allowing `PiggyTech Global Limited (PiggyVest)` to match an approved legal-name alias.

Lever's API lacks company metadata. A nonempty Lever board requires at least one actual posting's apply URL or description to mention the company name, an approved alias, or official domain. Every posting is checked, not just the displayed samples. Otherwise it remains `needs_review`, with sample apply URLs in the report. Descriptions used for this check stay in memory and are excluded from run logs. The report finishes with counts for confirmed with jobs, confirmed but empty, needs review, and not found.

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

The student profile includes internships and junior/entry-level engineering roles. Editable `seniority.exclude` terms in `data/filters.yaml` (senior, sr., lead, principal, staff, manager, head of, director, VP, vice president, chief) always exclude a job with an explicit reason. Intern, internship, graduate, trainee, junior, associate and entry-level engineering titles are strong matches when the location is eligible. Locations list African countries/cities and non-African regions in YAML; `Remote` without a region is check-fit. Matching is case-insensitive and uses word boundaries.

Unknown titles are excluded unless they have technical role evidence. `roles.description_titles` gates description-only matches, so company boilerplate about software cannot qualify an unrelated business role. `roles.maybe` retains adjacent Android/iOS, testing, DevOps, SRE and data-engineering roles for manual review; it does not automatically select them for tailoring. Default tailoring processes open, pending `include` jobs. After reviewing a check-fit role, `python -m pipeline.run_tailor --include-maybe --limit 1` also allows pending `maybe` jobs, selecting the oldest eligible one; the limit does not select a particular job. Filtering and tailoring run separately from the dashboard; scheduled orchestration is a later stage.

Reclassify existing jobs after changing these rules:

```powershell
.\.venv\Scripts\python.exe -m pipeline.run_filter
.\.venv\Scripts\python.exe -m pipeline.run_filter --apply
```

The first command previews category transitions. `--apply` updates only `filter_result` and `filter_reason`, guards against concurrent job changes, and reports category changes separately from reason-only updates. Full descriptions, resumes, statuses and notifications are preserved. The private audit log is gitignored `data/filter-runs.jsonl`; rerunning unchanged rules makes no updates.

```powershell
.\.venv\Scripts\python.exe -m pipeline.run_fetch --concurrency 4
.\.venv\Scripts\python.exe -m pipeline.run_fetch --concurrency 4
```

The first successful fetch backfills existing jobs with notifications suppressed. An unchanged second run reports zero new jobs. The summary distinguishes stored, new notifiable, edited, duplicate, and closed jobs. Run records go to Supabase and gitignored `data/fetch-runs.jsonl`. Exit codes: `0` successful, `1` source errors, `2` setup/configuration failure. Full snapshots commit atomically; one failing source does not stop the others. Nothing is emailed or tailored in this stage.

`pipeline/src/pipeline/` contains the CLIs and six isolated adapters. Fetches return complete provider JSON, including verified description requests and SmartRecruiters pagination/details. Tests mock all external HTTP using recorded ATS fixtures; storage semantics execute against local PostgreSQL. [Fixture notes](pipeline/tests/fixtures/README.md) list verified endpoints and real-job filter samples.

The [Stage 5 private dashboard](dashboard/README.md) is a Next.js app in `dashboard/`. Apply its forward migration and configure the single owner before signing in. Stage 6 sends new-job and health emails. Stage 7 adds retention cleanup; schedule its command once per day using Windows Task Scheduler or your existing pipeline scheduler.

## Stage 6 email notifications and health alerts

Apply [the Stage 6 migration](supabase/migrations/20261019000100_stage6_notifications_health.sql) after Stage 5, then apply the [include-only notification migration](supabase/migrations/20261019000200_stage6_include_email_only.sql). Set `RESEND_API_KEY`, `RESEND_FROM_EMAIL` to a sender verified in Resend, `NOTIFY_EMAIL`, and `DASHBOARD_URL`. `NOTIFY_RESUME_GRACE_MINUTES` defaults to 30 and can be set from 0 to 1440. New include jobs wait for a ready/failed resume or this grace period, whichever comes first. Maybe jobs never trigger an email by themselves; when an include email is sent, the final line reports how many check-fit roles are on the dashboard.

Preview without sending or writing to Supabase:

```powershell
.\.venv\Scripts\python.exe -m pipeline.run_notify --dry-run
```

Send notifications and evaluate source health:

```powershell
.\.venv\Scripts\python.exe -m pipeline.run_notify
```

Each run sends at most one new-job email for all eligible include jobs, lists at most 15, and links to `/jobs?new=1`. It sends no new-job email when there are no eligible include jobs, even if check-fit jobs exist. A durable private attempt and Resend idempotency key make retries safe if delivery succeeds while the database acknowledgment fails. Resend retains idempotency keys for 24 hours, so automated retries after a prolonged outage beyond that window cannot guarantee deduplication. Successfully emailed include jobs and accompanying check-fit jobs are added to the dashboard's notification membership; only emailed include jobs receive `notified_at`. Failed sends remain eligible for retry. Health alerts are separate, aggregate current issues, throttle each issue for 24 hours, and send a recovery notice when an issue clears. Health checks cover repeated source failures, repeated unexpected zero-job snapshots, careers-page shrink warnings, no successful source run for three hours, and three consecutive failed tailoring attempts. Stage 7's independent heartbeat is recorded in the private heartbeats table after applying its orchestration migration.

The local run log is gitignored at `data/notify-runs.jsonl`; it records counts and delivery state, never descriptions or secrets. All Resend calls are mocked in tests; live delivery requires your Resend account and verified sender.

## Stage 7 retention cleanup

Apply [the Stage 7 cleanup migration](supabase/migrations/20261020000100_stage7_cleanup.sql) after Stage 6. It permits archived resume state and nullable descriptions, adds private description-retention markers, and keeps source-identity tombstones so a purged closed listing cannot be inserted and emailed as new if it reappears in a later fetch.

Retention ages are editable in `data/cleanup.yaml` (60 days for closed `new`/`skipped` jobs; 30 days for inactive `applied`/`rejected`/`silent` applications and open `skipped` descriptions). Preview first, then run the command daily:

```powershell
.\.venv\Scripts\python.exe -m pipeline.cleanup --dry-run
.\.venv\Scripts\python.exe -m pipeline.cleanup
```

The cleanup command reads `.env` for Supabase credentials. In Windows Task Scheduler, create a daily task that runs the second command with the repository as its working directory. Dry-run performs no database or Storage mutations. Closed jobs are deleted only after their tailored file path is durably recorded for retry; applied/rejected/silent jobs keep their row, notes, status, and history while descriptions and tailored resume are removed and resume status becomes `archived`. Open skipped jobs keep their row and resume but have descriptions cleared after the configured age. Interview and offer jobs are left untouched. A private run log at `data/cleanup-runs.jsonl` records counts and sanitized failures.

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

Resume requests use compact JSON and evidence references to avoid repeating facts. Deterministic heading rules retain the job title, responsibilities, requirements, qualifications and tech stack while dropping company, benefits, equal-opportunity and application-process sections. Unknown prose is kept conservatively. If the complete redacted request plus reserved response still exceeds the configured budget, section tails are truncated in this order: unknown prose, overview, responsibilities, requirements/qualifications/tech stack, then an embedded title. The separate job title is always retained. Truncation is reported in the private run log and artifact report. Note options are regenerated after trimming; gap quotes must occur in the exact compacted, redacted description sent to the model. Full database descriptions are unchanged.

Requests require space for a full response (at least 2048 tokens), and fail before HTTP if master facts and the minimum response cannot fit even after description compaction. Set the budget to your account's supported quota; a confirmed 8K quota uses `LLM_MAX_TOKENS_PER_MINUTE=8000`. Groq GPT-OSS 20B/120B uses low reasoning effort, supported by the [Groq Chat API](https://console.groq.com/docs/api-reference). Explicit output-limit failures retain the master fallback without identical retries. See [resume setup and recovery](data/RESUMES.md).

All provider SDK calls share a thread-safe token budget within the run's HTTP session, including concurrent page sources and retries. `LLM_MAX_TOKENS_PER_MINUTE` defaults to 6000 and must be a positive integer. A rolling 60-second window reserves estimated prompt tokens (UTF-8 bytes/3 plus 256 tokens of framing headroom) and the full allowed completion, then reconciles against actual returned usage. The completion cap is at most 4096 and is reduced when necessary to fit the configured budget; a prompt exceeding that budget fails visibly without truncating source facts. Missing usage or transport failures retain the reservation. Estimates are not exact model tokenization; separate processes and other applications sharing the organization quota need their own coordination.

HTTP 429 has at most **three retries** (four HTTP attempts). Retries wait for the greater of exponential backoff (1, 2, 4 seconds) and `Retry-After`, including fractional seconds or an HTTP date, without shortening the server's delay. SDK automatic retries are disabled; exhausted rate limits stop outer JSON-validation retries as well. [Groq's rate-limit documentation](https://console.groq.com/docs/rate-limits) describes the server headers and organization-level quotas. An exhausted page extraction records an error and retains the previous baseline so the same change is retried on the next run. Resume tailoring instead follows its existing failed-status/master-file fallback path. Retry attempts are recorded; their unavailable usage makes the cost estimate null.

Enabled companies with a `careers_url` also get a `careers_page` source whenever ATS detection is empty, `needs_review`, `not_found`, or unknown. Older entries without `ats_job_count` retain this fallback until detection verifies a positive count. Confirmed ATS sources keep their adapter, including confirmed empty boards, so a confirmed empty board can run alongside its careers monitor. A confirmed ATS with open jobs needs no careers fallback. Run the same `pipeline.run_fetch` command; `--feeds-file PATH` defaults to [data/feeds.yaml](data/feeds.yaml).

Plain HTTP runs first. Under 100 characters of cleaned text or a loading/JavaScript shell triggers Chromium, except explicit no-vacancies messages. The successful method is cached in private Supabase state. Every page has a 45-second wall timeout, bounded redirects/retries, an 8 MB response cap, robots checks on each target, and the shared host limiter (at least one second, or a longer robots crawl delay). Browser GETs, including scripts and redirects, are fulfilled through that same client; non-GET requests, service workers, sockets, images, media, and fonts are blocked.

Snapshots remove navigation, footer, hidden content, cookies, banners, carousels, timestamps, dates, and known tracking parameters. Listing reordering does not change the hash. Date-shaped job URL identifiers remain intact. A matching hash skips extraction. A change sends only added/replaced regions with two nearby text lines and local link context; a first successful run uses the cleaned page as backfill. Changed inputs over 60,000 characters fail visibly and keep the previous baseline.

The LLM returns a strict JSON array of `{title, location, apply_url, description_snippet}`; nullable fields are required and extra fields are rejected. Apply URLs must be absolute, appear in actual page links, and use the company's registrable domain or one of the known ATS domains listed above, including Breezy. Fabricated URLs, empty titles, and redirects outside those domains are rejected. Detail descriptions are fetched when permitted; failures retain the extracted snippet and record a warning. Each canonical job uses `source_type: careers_page` and a SHA-256 of its cleaned apply URL as `external_id`.

Empty/failed pages, unfinished shells, and snapshots below 40% of a previous snapshot of at least 200 characters record an error. A failed extraction does not advance the hash. Jobs, snapshot, method, usage, and warnings commit atomically; stale concurrent baselines are rejected. **Page/feed diffs do not infer job closure:** previous jobs are retained because a partial region or rolling feed cannot prove a vacancy disappeared. This assumption preserves Stage 2 status, dedupe, backfill, and notification suppression.

`data/feeds.yaml` is empty until you supply verified Nigeria/Africa job-only URLs. Each entry requires `name`, `company`, `domain`, `url`, `enabled`, `verified`, and `scope` (`nigeria` or `africa`). Enable only after checking a real response. The adapter validates every response before storage: RSS/Atom or JSON with a root array or `jobs`/`items` array, using `title`, `apply_url`/`url`, nullable `location`, and `description_text`/`content_text`/`content_html`/`description`. Unexpected HTML, malformed XML/JSON/items, and a previously nonempty feed returning zero are errors. No feed URLs are guessed.

On 2026-10-10, TechCabal's jobs page embedded Airtable; its advertised feed and Techpoint Africa's advertised feed returned general news, not job-only postings. No job-specific RSS/JSON feed was verified for either, so both remain disabled. [Recorded verification](pipeline/tests/fixtures/stage3-verification.json) includes the real responses checked. Documented LLM endpoints returned 401 without credentials; authenticated generation remains to be verified with your configuration. The owner reported applying the Stage 3 migration on 2026-10-12; hosted database state has not been independently rechecked here.

Tests include controlled changes built from recorded job facts, recorded public HTML, mock Playwright, real PostgreSQL transactions/RLS, mocked Resend delivery, and optional actual Chromium rendering over mocked HTTP. To include the browser check, set `RUN_CHROMIUM_TESTS=1` before running pytest with an installed browser. Resume setup/tailoring is covered in [Stage 4](data/RESUMES.md); scheduled orchestration belongs to Stage 7.

## Stage 7 scheduling and orchestration

Apply [the Stage 7 orchestration migration](supabase/migrations/20261022000100_stage7_orchestration.sql) after Stage 6. It creates a private `heartbeats` table writable only by `service_role`. The ordered entry point is:

```powershell
.\.venv\Scripts\python.exe -m pipeline.run_all
```

It runs fetch, tailoring, job notifications, and a separate health evaluation in order. A failed step is logged and does not block later stages. The run writes a heartbeat after health evaluation; inability to persist that heartbeat makes the run fail. `python -m pipeline.run_all --dry-run` validates local inputs and source configuration, previews pending job/health notifications read-only, and makes no provider calls, emails, or database writes. Its run log is still written locally. Dry-run intentionally does not fetch postings or tailor resumes.

The main GitHub Actions workflow runs at minute 7 of every hour and supports manual dispatch. It serializes runs, uses Python 3.12, installs Chromium only if enabled careers-page sources need it, and uploads sanitized JSONL logs after failure. The first run step after Python setup checks required configuration, prints the Python version, and lists only the names of missing settings before failing. After dependency installation, a read-only Storage listing reports whether all four private resume input objects exist; it does not download or print their contents. The independent daily `Job Radar heartbeat check` workflow reads the latest heartbeat and sends a throttled health email if it is missing or older than three hours. A separate daily retention workflow runs the existing idempotent cleanup command.

Create these **GitHub Actions repository secrets** under Settings → Secrets and variables → Actions → New repository secret:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_KEY` (service-role key; keep private)
- `LLM_PROVIDER` (`openai`, `anthropic`, or `groq`)
- `LLM_API_KEY`
- `LLM_MODEL`
- `RESEND_API_KEY`
- `RESEND_FROM_EMAIL`
- `NOTIFY_EMAIL`
- `DASHBOARD_URL`

These nine are required for the hourly workflow. The same Supabase secrets are used by daily cleanup; the daily health check also needs the three Resend secrets. Optional hourly workflow secrets are `LLM_MAX_TOKENS_PER_MINUTE` (default `6000`), `LLM_MAX_CALLS_PER_RUN` (default `50`), `LLM_INPUT_USD_PER_MILLION`, `LLM_OUTPUT_USD_PER_MILLION`, `RESUME_MAX_PER_RUN` (default `3`), `NOTIFY_RESUME_GRACE_MINUTES` (default `30`), and `CAREERS_MAX_FETCHES_PER_RUN` (default `20`). The resume bucket name is fixed as `radar-resumes`; it is not a secret.

Gitignored resume inputs are read from the existing private `radar-resumes` Supabase Storage bucket. Apply [the private input MIME migration](supabase/migrations/20261023000100_stage7_private_resume_inputs.sql) once, then upload the reviewed local JSON, original DOCX, generated template, and layout metadata:

```powershell
.\.venv\Scripts\python.exe -m pipeline.resume.upload_inputs
```

The command reads `data/master_resume.json`, `data/master_resume.docx`, `data/resume_template.docx`, and `data/resume_layout.json`, then stores them under `pipeline-inputs/` in the private bucket. It prints filenames and upload status only. GitHub Actions uses `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` to download those four objects into an OS temporary directory before the stages run, passes those paths to tailoring, and removes the directory when the run exits. The bucket remains private and its client-facing policy still denies access. Remove the old `MASTER_RESUME_JSON_B64`, `MASTER_RESUME_DOCX_B64`, `RESUME_TEMPLATE_DOCX_B64`, and `RESUME_LAYOUT_JSON_B64` GitHub secrets; they are no longer used.

GitHub may delay scheduled workflows during high load and may disable them after prolonged repository inactivity. The daily heartbeat check detects a stopped main workflow, provided the daily workflow itself still runs. Keep the repository active with periodic commits and use `workflow_dispatch` to run the main workflow when needed. A heartbeat proves the pipeline reached its final database write; it does not guarantee every source succeeded. Check `data/run-all-runs.jsonl`, `data/fetch-runs.jsonl`, `data/tailor-runs.jsonl`, `data/notify-runs.jsonl`, and `data/health-runs.jsonl` locally or download the failure artifact. Logs contain counts and sanitized status/error types, not job descriptions or credentials.

`LLM_MAX_CALLS_PER_RUN` (default 50) caps provider HTTP attempts across careers extraction and tailoring, including retries. `CAREERS_MAX_FETCHES_PER_RUN` (default 20) caps careers-page sources attempted; excess configured pages are reported and deferred while ATS and verified-feed sources continue. Token pacing remains separately controlled by `LLM_MAX_TOKENS_PER_MINUTE`.

## Operating the system

- Add a company to `data/companies.yaml` with its official domain, country, enabled state, and—only when verified—its careers URL or ATS settings. Add feed URLs only after verifying a real job-only response in `data/feeds.yaml`.
- Re-run the detector with `python -m pipeline.detect_ats`; inspect the printed summary and gitignored `data/detection-runs.jsonl`, then use `python -m pipeline.detect_ats --write` to persist verified ATS fields.
- Review `needs_review` entries and sample evidence in `data/companies.yaml` and `data/detection-runs.jsonl`. Resolve ownership manually before enabling an uncertain ATS board.
- Read source, tailoring, notification, health, and orchestration JSONL run logs under `data/`. GitHub uploads these sanitized logs as an artifact when the main workflow fails.
- Temporarily disable a source by setting its company `enabled: false` in `data/companies.yaml`, or its feed `enabled: false` in `data/feeds.yaml`, then commit the change. The next source sync disables that registry entry without deleting its history.
