import { it, expect } from "vitest";
import { silentDays, safeApplyUrl, updateSchema } from "@/lib/model";
import { parseFilters, safeSearch } from "@/lib/filters";
it("does not infer unknown historic Applied dates", () => {
  expect(
    silentDays(
      { status: "applied", status_changed_at: null },
      "2026-10-25T12:00:00Z",
      21,
    ),
  ).toBeNull();
});
it("rejects immutable edits, malformed statuses and unsafe apply links", () => {
  expect(
    updateSchema.safeParse({
      expected_status: "new",
      status: "applied",
      title: "Edited",
    }).success,
  ).toBe(false);
  expect(
    updateSchema.safeParse({ expected_status: "new", status: "invented" })
      .success,
  ).toBe(false);
  expect(safeApplyUrl("javascript:alert(1)")).toBeNull();
  expect(safeApplyUrl("https://user:pass@example.test")).toBeNull();
});
it("keeps default closed/backfill visibility off and requires explicit maybe review", () => {
  expect(parseFilters({})).toMatchObject({
    closed: false,
    backfill: false,
    fit: "include",
    page: 1,
  });
  expect(parseFilters({ new: "1" })).toMatchObject({
    notification: true,
    fit: "relevant",
  });
  expect(safeSearch("x),status.eq.offer%")).not.toContain(")");
});
