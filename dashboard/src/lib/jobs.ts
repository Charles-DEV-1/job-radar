import "server-only";
import type { SupabaseClient } from "@supabase/supabase-js";
import {
  jobSchema,
  detailSchema,
  historySchema,
  STATUSES,
  LIST_COLUMNS,
  type JobStatus,
} from "./model";
import { safeSearch, type Filters } from "./filters";

export const PAGE_SIZE = 20;
export class DataError extends Error {
  constructor() {
    super("Jobs could not be loaded. Please try again.");
  }
}

export async function listJobs(client: SupabaseClient, filters: Filters) {
  let ids: string[] | null = null;
  if (filters.notification) {
    const batch = await client
      .from("notification_batches")
      .select("id")
      .order("sent_at", { ascending: false })
      .order("id")
      .limit(1)
      .maybeSingle();
    if (batch.error) throw new DataError();
    if (!batch.data) ids = [];
    else {
      const members = await client
        .from("notification_jobs")
        .select("job_id")
        .eq("notification_id", batch.data.id);
      if (members.error) throw new DataError();
      ids = members.data.map((r: { job_id: string }) => r.job_id);
    }
  }
  const counts = Object.fromEntries(STATUSES.map((s) => [s, 0])) as Record<
    JobStatus,
    number
  >;
  if (ids?.length === 0) return { jobs: [], total: 0, counts };
  function query(columns: string, head = false) {
    let q = client.from("jobs").select(columns, { count: "exact", head });
    if (!filters.closed) q = q.is("closed_at", null);
    if (!filters.backfill) q = q.eq("backfill", false);
    q =
      filters.fit === "relevant"
        ? q.in("filter_result", ["include", "maybe"])
        : q.eq("filter_result", filters.fit);
    if (filters.company) q = q.eq("company", filters.company);
    if (filters.from) q = q.gte("first_seen_at", filters.from + "T00:00:00Z");
    if (filters.to)
      q = q.lt(
        "first_seen_at",
        new Date(
          Date.parse(filters.to + "T00:00:00Z") + 86400000,
        ).toISOString(),
      );
    const text = safeSearch(filters.q);
    if (text) q = q.or(`title.ilike.%${text}%,company.ilike.%${text}%`);
    if (ids) q = q.in("id", ids);
    return q;
  }
  let items = query(LIST_COLUMNS)
    .order("first_seen_at", { ascending: false })
    .order("id");
  if (filters.status !== "all") items = items.eq("status", filters.status);
  const [result, ...totals] = await Promise.all([
    items.range((filters.page - 1) * PAGE_SIZE, filters.page * PAGE_SIZE - 1),
    ...STATUSES.map((s) => query("id", true).eq("status", s)),
  ]);
  if (result.error || totals.some((r) => r.error)) throw new DataError();
  STATUSES.forEach((s, i) => (counts[s] = totals[i].count ?? 0));
  return {
    jobs: jobSchema.array().parse(result.data),
    total: result.count ?? 0,
    counts,
  };
}
export async function companies(client: SupabaseClient): Promise<string[]> {
  const names = new Set<string>();
  let offset = 0;
  while (true) {
    const result = await client
      .from("jobs")
      .select("company")
      .order("id")
      .range(offset, offset + 499);
    if (result.error) throw new DataError();
    result.data.forEach((r: { company: string }) => names.add(r.company));
    if (result.data.length < 500) break;
    offset += 500;
  }
  return [...names].sort();
}
export async function lastCheck(
  client: SupabaseClient,
): Promise<string | null> {
  const result = await client
    .from("source_runs")
    .select("completed_at")
    .eq("status", "ok")
    .order("completed_at", { ascending: false })
    .limit(1)
    .maybeSingle();
  if (result.error) throw new DataError();
  return result.data?.completed_at ?? null;
}
export async function getJob(client: SupabaseClient, id: string) {
  const result = await client
    .from("jobs")
    .select(LIST_COLUMNS + ",description_text,match_notes")
    .eq("id", id)
    .maybeSingle();
  if (result.error) throw new DataError();
  return result.data ? detailSchema.parse(result.data) : null;
}
export async function statusHistory(client: SupabaseClient, id: string) {
  const result = await client
    .from("status_history")
    .select("id,job_id,status,changed_at")
    .eq("job_id", id)
    .order("changed_at", { ascending: false });
  if (result.error) throw new DataError();
  return historySchema.array().parse(result.data);
}
