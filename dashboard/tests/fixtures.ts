import type { Job } from "@/lib/model";
export const ID = "00000000-0000-4000-8000-000000000001";
export function job(changes: Partial<Job> = {}): Job {
  return {
    id: ID,
    company: "Recorded test company",
    title: "Backend Engineer",
    location_raw: "Lagos, Nigeria",
    remote_flag: false,
    source: "greenhouse",
    first_seen_at: "2026-10-01T12:00:00Z",
    posted_at: null,
    filter_result: "include",
    filter_reason: "Engineering role in Nigeria",
    resume_status: "ready",
    resume_error: null,
    status: "new",
    status_changed_at: "2026-10-01T12:00:00Z",
    notes: null,
    apply_url: "https://example.test/apply",
    closed_at: null,
    backfill: false,
    ...changes,
  };
}
