# Stage 2 database setup

Use a Supabase project dedicated to this private Job Radar instance. Run [the Stage 2 migration](migrations/20261006000100_stage2_fetch_storage.sql) in the project's SQL Editor, or apply it with your configured Supabase migration tooling. The pipeline service key can call ingestion RPCs after migration; it cannot install SQL migrations by itself.

Configure `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` in the repository's gitignored `.env`, or export them as environment variables. Use the service-role JWT or a Supabase secret key, not an anon/publishable key. The fetch CLI loads `.env` without overriding existing environment variables. No other credentials are needed by Stage 2.

RLS is enabled on `sources`, `source_runs`, `jobs`, and `job_sources`. Anonymous and authenticated roles have neither table access nor RPC execution grants. The service role owns ingestion access. Dashboard authentication and user policies are a later stage.

## Storage rules

- A source is keyed by ATS and slug. Registry sync mirrors the full confirmed company list, including disabling removed or disabled sources.
- `radar_start_run` records an attempted run before network fetching. An unfinished run remains an error with `Run did not complete`.
- `radar_commit_snapshot` is one transaction: dedupe, insert/update, alternate URLs, closure, run results, and health fields either all commit or all roll back. Database writes serialize with a transaction advisory lock; network fetching remains concurrent. Replaying the same completed run ID returns its saved counts.
- Same-source identity uses the external ID, including across title/location edits. Cross-source identity uses the SHA-256 of normalized company, title, and location. Separate same-source requisitions with different IDs are preserved even if their titles and locations match.
- `job_sources` preserves alternate external IDs and apply URLs. A job closes only after every listing is closed. Reappearing listings reopen the same job without generating a new job or resetting status/notes/notification state.
- Content updates preserve `first_seen_at`, status, notes, resume fields, `backfill`, and existing `notified_at`. Primary-source content is preferred to avoid oscillating between provider description formats. If the primary listing disappears, an active alternate apply URL is used.
- The first **successful** snapshot is backfill, even if an earlier attempt failed. Existing jobs get `backfill=true`, `status=new`, and `notified_at` set. An empty first successful snapshot also establishes the baseline.
- `new_jobs` counts newly inserted, nonexcluded jobs on later successful source runs. `inserted_jobs` also includes backfill and excluded jobs. Edited, duplicate, and reopened jobs never increase `new_jobs`.
- Excluded jobs are stored and marked notification-suppressed. Changing a posting or filter rule cannot make those old jobs newly notifiable. Future tailoring/email queries must also require `filter_result <> 'exclude'`, `notified_at is null`, `backfill=false`, and `closed_at is null`.
- Only complete, valid snapshots close missing jobs. A failed fetch, malformed posting, incomplete Greenhouse list, or failed SmartRecruiters detail/pagination request leaves the previous snapshot intact. Local JSONL records provide a fallback when Supabase is unavailable.

Health alerts, email delivery, resume tailoring, careers-page monitoring, and the dashboard are not implemented in Stage 2.

## Stage 3 forward migration

Apply [20261010000100_stage3_careers_monitor.sql](migrations/20261010000100_stage3_careers_monitor.sql) after the Stage 2 migration. It adds canonical `source_type`, monitor registry identities, private `source_monitor_state`, and per-run usage/warning metadata. Existing ATS identities and ingestion behavior are preserved.

The new `radar_monitor_state`, `radar_commit_monitor`, and `radar_run_metadata` RPCs are invoker functions executable only by `service_role`. Monitor state has RLS enabled with no anonymous/authenticated grants. Snapshot text, links, method, and cached canonical jobs remain private. `radar_commit_monitor` checks the expected previous hash and commits jobs/state/metadata in one transaction. Replay returns the saved result; a failed or stale commit keeps the previous state.

Registry sync now mirrors the entire ATS/page/feed configuration. Careers pages use a stable hash of the official domain and cleaned careers URL as their slug; feeds use a hash of the supplied URL. Missing monitored listings are retained rather than closed from an incremental diff. Also apply [20261012000100_stage3_sync_safe_updates.sql](migrations/20261012000100_stage3_sync_safe_updates.sql), which fixes source sync for the hosted safe-update guard.

## Stage 4 private resume tailoring

Apply [20261012000200_stage4_resume_tailoring.sql](migrations/20261012000200_stage4_resume_tailoring.sql) after the prior migrations. It adds private per-job resume runs, claims, filenames, errors and match notes. `radar_claim_tailor` claims one eligible pending/open job with a 15-minute lease; `radar_finish_tailor` validates the claim and original content hash before accepting its file. Both RPCs remain service-role-only invoker functions. Existing job, notification and ingestion behavior is preserved.

The migration creates the private `radar-resumes` bucket with an 8 MiB DOCX/PDF limit. A restrictive Storage objects policy blocks direct anonymous/authenticated access even if another permissive policy exists. The pipeline service issues short-lived signed download URLs. No dashboard policies or email are implemented. Follow [the resume guide](../data/RESUMES.md) for offline facts review, template checks and later LLM configuration.

The Stage 4 migration and bucket/RPC behavior are tested locally with PGlite and the SDK. The migration must still be applied to the project configured by `SUPABASE_URL`; local checks do not establish hosted deployment or visual acceptance with the owner's PDF/DOCX.

## Stage 5 private dashboard

Apply [20261014000100_stage5_private_dashboard.sql](migrations/20261014000100_stage5_private_dashboard.sql) after Stage 4. It adds a private owner allowlist, owner-only job/source-run/history policies, column-limited `status`/`notes` updates, atomic status history and explicit future notification membership. Existing ingestion RPCs and restrictive private Storage access are retained.

Until the allowlist is configured, authenticated clients see no dashboard data. Follow [dashboard setup](../dashboard/README.md): configure `ALLOWED_EMAIL`, create the sole confirmed Auth user, disable public signups, then run `npm.cmd run setup` from `dashboard`. That service-role helper calls `radar_configure_dashboard` from the environment and uploads the untailored master DOCX privately. Hosted migration application, account setup and Vercel/phone acceptance must be verified separately from local migration tests.

## Stage 6 notifications and health events

Apply [20261019000100_stage6_notifications_health.sql](migrations/20261019000100_stage6_notifications_health.sql) after Stage 5. It adds a private durable job-notification attempt/outbox, atomic completion into `notification_batches`/`notification_jobs`, and the private `health_events` issue state table. Only `service_role` can read or write these tables or execute notification RPCs. RLS remains enabled with no client policies. Resend delivery uses a persistent idempotency key so a retry after an uncertain network/database result cannot deliver the same email twice.

The migration must be applied to the same project configured by `SUPABASE_URL`. `python -m pipeline.run_notify --dry-run` previews job and health emails without changing database state; normal execution requires Resend credentials and a verified sender address.
