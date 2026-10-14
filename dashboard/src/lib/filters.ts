import { STATUSES, type JobStatus } from "./model";
export type SearchParams = Record<string, string | string[] | undefined>;
export type Fit = "include" | "relevant" | "maybe" | "exclude";
export interface Filters {
  q: string;
  company: string;
  status: JobStatus | "all";
  fit: Fit;
  from: string;
  to: string;
  closed: boolean;
  backfill: boolean;
  notification: boolean;
  page: number;
}
const first = (v: string | string[] | undefined) =>
  typeof v === "string" ? v : "";
function date(v: string): string {
  return /^\d{4}-\d{2}-\d{2}$/.test(v) &&
    Number.isFinite(Date.parse(v + "T00:00:00Z"))
    ? v
    : "";
}
export function parseFilters(params: SearchParams): Filters {
  const status = first(params.status);
  const fit = first(params.fit);
  const notification = first(params.new) === "1";
  return {
    q: first(params.q).trim().slice(0, 100),
    company: first(params.company).slice(0, 200),
    status: STATUSES.includes(status as JobStatus)
      ? (status as JobStatus)
      : "all",
    fit: ["include", "relevant", "maybe", "exclude"].includes(fit)
      ? (fit as Fit)
      : notification
        ? "relevant"
        : "include",
    from: date(first(params.from)),
    to: date(first(params.to)),
    closed: first(params.closed) === "1",
    backfill: first(params.backfill) === "1",
    notification,
    page: Math.min(
      100000,
      Math.max(1, Math.floor(Number(first(params.page)) || 1)),
    ),
  };
}
export function filtersHref(
  filters: Filters,
  overrides: Partial<Filters> = {},
): string {
  const values = { ...filters, ...overrides };
  const p = new URLSearchParams();
  for (const key of ["q", "company", "from", "to"] as const)
    if (values[key]) p.set(key, values[key]);
  if (values.status !== "all") p.set("status", values.status);
  p.set("fit", values.fit);
  if (values.closed) p.set("closed", "1");
  if (values.backfill) p.set("backfill", "1");
  if (values.notification) p.set("new", "1");
  if (values.page > 1) p.set("page", String(values.page));
  return "/jobs?" + p.toString();
}
export function safeSearch(value: string): string {
  return value
    .replace(/[^\p{L}\p{N}\s.+-]/gu, " ")
    .replace(/\s+/g, " ")
    .trim();
}
