import { z } from "zod";

export const STATUSES = [
  "new",
  "applied",
  "interview",
  "rejected",
  "silent",
  "offer",
  "skipped",
] as const;
export type JobStatus = (typeof STATUSES)[number];
export const STATUS_LABELS: Record<JobStatus, string> = {
  new: "New",
  applied: "Applied",
  interview: "Interview",
  rejected: "Rejected",
  silent: "Silent",
  offer: "Offer",
  skipped: "Skipped",
};
export const jobSchema = z.object({
  id: z.uuid(),
  company: z.string(),
  title: z.string(),
  location_raw: z.string(),
  remote_flag: z.boolean(),
  source: z.string(),
  first_seen_at: z.string(),
  posted_at: z.string().nullable(),
  filter_result: z.enum(["include", "maybe", "exclude"]),
  filter_reason: z.string(),
  resume_status: z.enum(["pending", "ready", "failed", "archived"]),
  resume_error: z.string().nullable(),
  status: z.enum(STATUSES),
  status_changed_at: z.string().nullable(),
  notes: z.string().nullable(),
  apply_url: z.string(),
  closed_at: z.string().nullable(),
  backfill: z.boolean(),
});
export type Job = z.infer<typeof jobSchema>;
export const LIST_COLUMNS = Object.keys(jobSchema.shape).join(",");
export const historySchema = z.object({
  id: z.uuid(),
  job_id: z.uuid(),
  status: z.enum(STATUSES),
  changed_at: z.string(),
});
export type History = z.infer<typeof historySchema>;
export const notesSchema = z.object({
  matched: z.array(
    z.object({ requirement: z.string(), evidence_ids: z.array(z.string()) }),
  ),
  gaps: z.array(z.string()),
});
export const detailSchema = jobSchema.extend({
  description_text: z.string().nullable(),
  match_notes: notesSchema.nullable(),
});
export type JobDetail = z.infer<typeof detailSchema>;
export const updateSchema = z
  .object({
    expected_status: z.enum(STATUSES),
    status: z.enum(STATUSES).optional(),
    notes: z.string().max(5000).optional(),
  })
  .strict()
  .refine((v) => v.status !== undefined || v.notes !== undefined);

export function safeApplyUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) &&
      !url.username &&
      !url.password
      ? url.href
      : null;
  } catch {
    return null;
  }
}
export function relativeTime(value: string, now: string): string {
  const seconds = Math.max(0, (Date.parse(now) - Date.parse(value)) / 1000);
  if (!Number.isFinite(seconds)) return "date unavailable";
  if (seconds < 60) return "just now";
  const unit =
    seconds < 3600
      ? "minute"
      : seconds < 86400
        ? "hour"
        : seconds < 604800
          ? "day"
          : seconds < 2592000
            ? "week"
            : "month";
  const divisor = {
    minute: 60,
    hour: 3600,
    day: 86400,
    week: 604800,
    month: 2592000,
  }[unit];
  return new Intl.RelativeTimeFormat("en", { numeric: "always" }).format(
    -Math.floor(seconds / divisor),
    unit,
  );
}
export function silentDays(
  job: Pick<Job, "status" | "status_changed_at">,
  now: string,
  threshold: number,
): number | null {
  if (job.status !== "applied" || !job.status_changed_at) return null;
  const days = Math.floor(
    (Date.parse(now) - Date.parse(job.status_changed_at)) / 86400000,
  );
  return Number.isFinite(days) && days >= threshold ? days : null;
}
