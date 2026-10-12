# Stage 2 fixtures and field mappings

Stage 2's new description requests were verified live on 2026-10-06 before implementation. `stage2-verification.json` records their URLs and response keys:

- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/moniepoint/jobs?content=true` returned descriptions, departments, and offices.
- Workable: the documented `https://www.workable.com/api/accounts/fairmoney?details=true` redirects (302) to `https://apply.workable.com/api/v1/widget/accounts/fairmoney?details=true`, which returned HTTP 200 with full descriptions. The adapter requests that verified final URL directly. See [Workable's careers-page guide](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page).
- SmartRecruiters: the recorded posting's `ref`, `https://api.smartrecruiters.com/v1/companies/StandardBankGroup/postings/744000153508782`, returned `jobAd.sections`, `applyUrl`, and full posting fields.

The attempted Workable per-job URL under `/api/v2/accounts/` returned 404 and is not used. No adapter depends on that unverified pattern. Lever, Ashby, and Recruitee normalization uses the recorded full responses from Stage 1; M-KOPA's Ashby response was also refreshed on 2026-10-06 for real-job filter review.

| Field | Greenhouse | Lever | Ashby | Workable | Recruitee | SmartRecruiters detail |
| --- | --- | --- | --- | --- | --- | --- |
| External ID | `id` | `id` | `id` | `shortcode` | `id` | `id` |
| Title | `title` | `text` | `title` | `title` | `title` | `name` |
| Location | `location.name` | `categories.allLocations` / `location` | `location` + `secondaryLocations` | `locations`, else city/state/country | `locations`, else `location` | `location.fullLocation`, else city/region/country |
| Description | decoded `content` | opening/body, lists, additional | `descriptionHtml` / `descriptionPlain` | `description` | `description` + `requirements` | `jobAd.sections.*.text` |
| Posted at | `first_published` | `createdAt` epoch milliseconds | `publishedAt` | `published_on` | `published_at` | `releasedDate` |
| Department | `departments[].name` | category department/team | department/team | `department` | `department` | `department.name` / label |
| Employment | null if absent | `categories.commitment` | `employmentType` | `employment_type` | `employment_type_code` | `typeOfEmployment.label` |
| Remote | location rule if no explicit flag | `workplaceType` | `isRemote` / `workplaceType` | `telecommuting` | `remote` | `location.remote` |
| Apply URL | `absolute_url` | `applyUrl` / `hostedUrl` | `applyUrl` / `jobUrl` | `application_url` / URL | `careers_apply_url` / URL | `applyUrl` / `postingUrl` |

Company identity uses the already-confirmed company configuration consistently across sources. Missing/invalid posted dates remain null; no adapter substitutes the edit timestamp. `first_seen_at` comes from ingestion and is preserved on edits. Location tags and textual remote hints are driven by `data/filters.yaml`. Unexpected location shapes produce a string or empty string, and an empty location is classified `maybe` for an otherwise relevant role.

Collection fixtures retain two unmodified jobs and original metadata. Tests explicitly project complete small snapshots by adjusting the collection total, so the production completeness checks remain enabled. Provider failures, malformed fields, pagination, and other edge cases are constructed test scenarios. The SmartRecruiters second detail in the pagination test is explicitly synthetic; the first detail is the recorded response.

## Manual review of real job samples

Workable's recorded FairMoney response contains 45 location rows for 19 unique shortcodes. `workable-location-variants.json` keeps five unmodified rows illustrating repeated IDs across cities and countries. Normalization groups these by shortcode and retains a sorted union of locations before filtering and hashing. Differences in other posting fields fail the snapshot rather than silently choosing one row. Summary counts represent unique postings; failure records retain the received raw row count.

`real_filter_samples.json` retains real postings and expected results reviewed on 2026-10-06:

| Posting | Eligible locations | Expected |
| --- | --- | --- |
| M-KOPA Senior Backend Engineer | Nairobi and other listed African cities | include |
| M-KOPA Senior Backend Engineer | London and other listed European cities | exclude |
| M-KOPA Senior Backend Engineer - Asset Sales | Nairobi and other listed African cities | include |
| Moniepoint Senior Site Reliability Engineer | Remote, Nigeria | maybe |
| Moniepoint Analytics Manager | Remote, Portugal | exclude |

An engineering role's product area can contain "Sales" without making it a sales role. Explicit engineering titles take precedence over those business words; DevOps/SRE/data engineering remain `maybe`. Location eligibility uses posting locations, not casual references to African customers or company offices. No salary or paid/unpaid checks exist.

SQL integration tests execute the actual migration and RPCs under a local PostgreSQL service-role equivalent using PGlite. They verify baseline suppression, unchanged second runs, cross-source alternate URLs, closure/reopening, material edits, status/notes preservation, transaction rollback, stalled-run records, replay protection, source health, and RLS/execute permissions. The official Supabase SDK is additionally exercised through mocked HTTP, including JSON and void RPC responses, credential isolation, and redacted errors. Hosted verification is distinct from these local tests.

## Hosted acceptance check

After the user applied the migration through Supabase SQL Editor on 2026-10-06, the actual fetch CLI ran against verified Moniepoint (Greenhouse), M-KOPA (Ashby), and FairMoney (Workable) boards. Each source's first successful snapshot suppressed notifications. Two final consecutive runs completed successfully; the repeat inserted zero jobs and reported zero new notifiable jobs across all three sources.

`stage2-live-acceptance.json` records aggregate results only: 205 stored/backfilled jobs (150 Moniepoint, 36 M-KOPA, 19 FairMoney), zero pending notifiable jobs, and cleared source health errors. An unauthenticated jobs request returned 401; local SQL tests separately verify table RLS and role grants. Initial Workable redirect and duplicate-location failures were recorded in `source_runs`; successful retries recovered without losing the other sources' snapshots.

The hosted test used a separate gitignored company list; `data/companies.yaml` remains the editable example template. Public responses can change after this check. The acceptance record contains no project URL, credentials, or job descriptions. Final local validation: 249 tests passed, with all SQL checks included, plus Ruff lint/format and dependency compatibility checks.

Primary storage references: [Supabase Python RPC](https://supabase.com/docs/reference/python/rpc), [Supabase RLS](https://supabase.com/docs/guides/database/postgres/row-level-security), [PGlite PostgreSQL test engine](https://pglite.dev/docs/).
