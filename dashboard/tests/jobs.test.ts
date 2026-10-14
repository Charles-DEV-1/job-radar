import { it, expect, vi } from "vitest";
import type { SupabaseClient } from "@supabase/supabase-js";
import { listJobs } from "@/lib/jobs";
import { parseFilters } from "@/lib/filters";
it("never mistakes notification suppression timestamps for a sent notification", async () => {
  const from = vi.fn();
  const builder = {
    select: vi.fn().mockReturnThis(),
    order: vi.fn().mockReturnThis(),
    limit: vi.fn().mockReturnThis(),
    maybeSingle: vi.fn().mockResolvedValue({ data: null, error: null }),
  };
  from.mockReturnValue(builder);
  const result = await listJobs(
    { from } as unknown as SupabaseClient,
    parseFilters({ new: "1" }),
  );
  expect(result.jobs).toEqual([]);
  expect(from).toHaveBeenCalledExactlyOnceWith("notification_batches");
});
