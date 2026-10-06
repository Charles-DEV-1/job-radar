# Project goal
Poll job sources frequently, detect NEW relevant jobs, store them, generate a tailored resume per job, and email me ONLY when there is at least one new job. A dashboard shows jobs with an apply link, a tailored-resume download link, and status buttons.

# Scope (current phase)
- Nigerian companies and companies/startups across Africa, plus roles that are explicitly open to Nigeria/Africa.
- Role focus: software engineering, especially backend. Include internships. Do NOT try to filter by paid/unpaid or salary; I decide that myself.
- Global remote job feeds are OUT OF SCOPE for now. Design the source layer so they can be added later as new adapters.

# Hard rules
1. Never scrape LinkedIn, Indeed, or any site that forbids automated access. Only use public ATS endpoints, public RSS/JSON feeds, and company careers pages. Respect robots.txt on careers pages, send an honest User-Agent, rate-limit requests (max 1 request/second per host), and use timeouts and retries with backoff.
2. Never invent API endpoints. Verify every external endpoint against a real response before writing code that depends on it. If an endpoint cannot be verified, say so and leave the adapter disabled.
3. Every source adapter is isolated: one failing source must never stop the others. Failures are recorded, not swallowed.
4. No silent failure. Every source run writes a record (status, job count, error, duration). A source that errors repeatedly or suddenly returns zero jobs must trigger a health alert (built in a later stage).
5. The pipeline must be idempotent: running it twice in a row must not create duplicates or send duplicate emails.
6. The resume tailoring step may only rephrase, reorder, emphasize, and select from facts that already exist in my master resume. It must NEVER add employers, titles, dates, degrees, certifications, skills, tools, or metrics that are not in the master resume. This is enforced by code validation, not just by the prompt.
7. Secrets only come from environment variables. Never hardcode or log secrets, resume contents, or full job descriptions at INFO level.
8. The dashboard is private: authentication required, row-level security on, resume files accessed via short-lived signed URLs. No public access to my resume or job data.
9. Send an email ONLY if there is at least one new job in that run. Zero new jobs means zero emails. (Separate health-alert emails are allowed under the rules of the health stage.)
10. All code is typed, tested with pytest, and formatted with ruff. External HTTP is mocked in tests using recorded fixtures.

# Architecture (data flow)
sources (ATS adapters, careers-page monitor) -> normalize -> dedupe -> filter -> store in Supabase -> tailor resume (DOCX) -> upload to Supabase Storage -> email summary with dashboard link -> dashboard (status tracking)

# Canonical Job schema (all sources normalize to this)
source, source_company_slug, external_id, company, title, location_raw, location_tags, remote_flag, department, employment_type, description_text, description_html, apply_url, posted_at (nullable), first_seen_at, content_hash, filter_result ("include" | "maybe" | "exclude"), filter_reason, status.

# Working style
- Work in small, reviewable commits. Explain what you verified and what you could not verify.
- If a requirement is ambiguous, state your assumption in the PR/commit message and proceed with the safest option.
- Do not add features beyond the current stage.
