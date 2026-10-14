# Private Job Radar dashboard — Stage 5

Next.js App Router, TypeScript, Tailwind and Supabase. Desktop and mobile layouts follow the supplied Job Radar Dashboard PDF. Every displayed job and count comes from Supabase; the application has no demonstration jobs. Email delivery and scheduling remain later stages.

## Database and owner setup

1. Apply the existing Stage 2–4 migrations, then [20261014000100_stage5_private_dashboard.sql](../supabase/migrations/20261014000100_stage5_private_dashboard.sql) in your project's Supabase SQL Editor. This is a forward migration; do not rerun or edit previously applied migrations.
2. In Supabase Authentication, create your single user with the same email as `ALLOWED_EMAIL`, a password, and confirmed email. Disable public signups. This dashboard uses password sign-in and has no signup or magic-link flow. Keep your password out of environment files and Git.
3. Copy `.env.example` to `.env.local` inside **dashboard**, then fill the server-only variables below. The root pipeline `.env` is independent. A local `.env.local` prepared during implementation uses your chosen master-resume email; still supply the publishable/anon key.
4. Run the setup helper locally. It sets the SQL allowlist from `ALLOWED_EMAIL` and uploads the existing unmodified DOCX to the **private** resume bucket. It sends no email and creates no authentication account.

```powershell
cd dashboard
npm.cmd ci
npm.cmd run setup
npm.cmd run dev
```

Open `http://localhost:3000/login`. On your phone, use the deployed HTTPS URL rather than the computer's localhost address.

| Variable                   | Purpose                                                                                                                                           |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `SUPABASE_URL`             | Existing project's HTTPS URL.                                                                                                                     |
| `SUPABASE_PUBLISHABLE_KEY` | Publishable API key from Supabase Project Settings / API Keys.                                                                                    |
| `SUPABASE_ANON_KEY`        | Alternative legacy anon JWT; supply this only when not using a publishable key. Secret/service-role keys are rejected for session authentication. |
| `SUPABASE_SERVICE_KEY`     | Existing secret/service-role key, **server-only**, used for signing private downloads.                                                            |
| `ALLOWED_EMAIL`            | Your sole allowed, confirmed Supabase Auth account. Must match the SQL allowlist configured by setup.                                             |
| `SILENT_AFTER_DAYS`        | Positive integer, default `21`.                                                                                                                   |
| `MASTER_RESUME_PATH`       | Private bucket object, default `master/master_resume.docx`.                                                                                       |
| `MASTER_RESUME_FILE`       | **Local setup only**, default `../data/master_resume.docx`; do not add this local filesystem path to Vercel.                                      |

No variable needs a `NEXT_PUBLIC_` prefix. All Supabase access happens on the server using the [Supabase cookie-based SSR client](https://supabase.com/docs/guides/auth/server-side/creating-a-client?framework=nextjs). Missing owner/key configuration fails closed. Credentials, email, passwords, descriptions and notes are never logged.

## Vercel deployment

1. Import this repository into your Vercel account. Select **Next.js**, set **Root Directory to `dashboard`**, and use Node.js 24 (or a version supported by the installed Next.js release).
2. Add the variables above for the target environment, excluding `MASTER_RESUME_FILE`. Treat the service key as a sensitive, server-only environment variable. Never prefix it with `NEXT_PUBLIC_`.
3. Confirm the Stage 5 migration and local `npm.cmd run setup` succeeded for the **same** Supabase project, then deploy. Vercel's [Next.js deployment guide](https://vercel.com/docs/frameworks/full-stack/nextjs) covers Git imports and CLI deployment.
4. In Supabase Auth URL Configuration, set Site URL to your deployed HTTPS origin. No OAuth callback is needed for this password-only flow. Keep signup disabled.
5. Sign in and complete the acceptance checks below. Configure `DASHBOARD_URL` in the **pipeline's** environment when Stage 6 email is implemented.

Alternatively, with Vercel CLI authenticated locally, run `vercel` from `dashboard`, add the required environment variables to the linked project, and run `vercel --prod`. Vercel credentials/project linkage are not stored in the repository. Local build success is not evidence of deployment or hosted migration application.

## Access policies and private downloads

The migration starts **deny-all** until the service-role-only `radar_configure_dashboard` RPC configures the owner. `radar_dashboard_is_owner()` checks the actual `auth.users` ID, confirmed email, JWT email and private allowlist. Application checks independently enforce `ALLOWED_EMAIL`. An environment/SQL mismatch denies access.

Authenticated roles receive SELECT on jobs, status history, source runs and notification membership, with owner-only [RLS policies](https://supabase.com/docs/guides/database/postgres/row-level-security). They can UPDATE **only `status` and `notes`** on jobs. Clients cannot modify job facts, resume paths, history timestamps or notification membership. The existing pipeline's service-role ingestion stays intact. Status changes and their history insert commit atomically; repeating the same status or saving notes does not reset the application date.

All pages except `/login` verify authentication; protected API routes also verify the owner directly. Only static framework assets are exempt. Sessions use HTTP-only cookies, mutations require the same origin, and private responses disable caching. The service key is isolated in a `server-only` module. Job descriptions render as escaped text, never executable HTML.

`GET /api/jobs/{id}/resume` first reads the job through the signed-in, RLS-protected client, then issues a **60-second** Storage signed URL with download disposition. Stage 4's restrictive Storage policy stays in place; users cannot read bucket objects directly. A ready job downloads its validated DOCX path. Pending/failed jobs offer the uploaded unmodified master; a missing tailored file falls back to master when possible. Missing master storage is shown as a visible download error. See [Supabase's private download documentation](https://supabase.com/docs/guides/storage/serving/downloads).

## List behavior

- Jobs are ordered by `first_seen_at` descending with stable ID ties; pages contain 20 rows.
- Defaults show **include**, open, non-backfill jobs. Select “Include check fit roles” to explicitly review `maybe`; excluded roles are available only through their filter. Inclusion means role/location rules, not a guaranteed resume match. With today's dataset, an empty default view is possible.
- Company, status, title/company search, first-seen dates, closed and backfill toggles persist in the URL. Search punctuation that could alter a PostgREST expression is removed.
- Top counts use the current visibility/company/search/date/fit filters but ignore the selected status, so status navigation remains useful. Skipped is available in the status filter and every job's buttons.
- Posted age uses `posted_at` when available; the first-seen timestamp is also shown. Detail/history dates use Africa/Lagos.
- The Silent helper suggests a change after the configured number of days in Applied, using `status_changed_at`. Older statuses with unknown transition dates receive no invented age. The helper never changes a job automatically.
- `/jobs?new=1` uses explicit `notification_batches` / `notification_jobs` membership from the latest sent notification and defaults to include + maybe. No email is sent in Stage 5. Stage 6 must write this membership on successful delivery. Existing backfill/suppression `notified_at` values are not treated as email delivery; until a real batch exists, this view is empty. Other selected filters still apply.

## Verification

```powershell
cd dashboard
npm.cmd run typecheck
npm.cmd test
npm.cmd run format:check
npm.cmd run build
cd ..
.\.venv\Scripts\python.exe -m pytest pipeline/tests
.\.venv\Scripts\ruff.exe check pipeline/src pipeline/tests
```

Component tests cover successful/failed status updates, notes and master fallback indicators. Route tests cover unauthenticated/wrong-user rejection, CSRF, immutable edits, user-scoped job lookup, signed URL lifetime and fallback paths. PostgreSQL/PGlite tests execute the actual migration and verify RLS, column grants, immutable history, atomic status changes and direct Storage denial. External provider responses are mocked; no resume-generation calls are needed.

The optional visual harness at `tests/browser` uses synthetic test records outside the application route tree. Run `.\node_modules\.bin\vite.cmd --config tests/browser/vite.config.ts` from `dashboard` to inspect the actual card components and CSS at desktop/mobile sizes. It has no real login or persistence and is not deployed as an application route. During implementation, Chromium checked this harness at 1440 px and 390 px and the production server's logged-out page/API gates separately. Authenticated hosted behavior still requires the account and migration setup above.

Hosted acceptance, using your phone and an incognito browser:

1. Signed out: `/`, `/jobs`, a detail URL and both API routes must expose no jobs or files. APIs reject access; pages redirect to `/login`.
2. Sign in with the configured account. Another confirmed account must still be denied by both the app and Supabase RLS.
3. Change New → Applied; refresh and check the status and single history entry. Save notes and confirm the application date/history count do not change.
4. Download a ready job's DOCX; download master for a pending/failed job. Check the actual files. A signed URL must expire after approximately 60 seconds.
5. Check search/filters, closed/backfill toggles and mobile layout. `/jobs?new=1` remains empty until Stage 6 records a real notification.
6. Sign out and revisit the same links; no private information should appear. Do not claim these hosted/phone checks from local tests alone.
