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

Registry sync now mirrors the entire ATS/page/feed configuration. Careers pages use a stable hash of the official domain and cleaned careers URL as their slug; feeds use a hash of the supplied URL. Missing monitored listings are retained rather than closed from an incremental diff. The owner reported applying the Stage 3 migration on 2026-10-12; hosted database state has not been independently rechecked here.
