import Link from "next/link";
import { requirePageSession } from "@/lib/auth";
import { silentThreshold } from "@/lib/config";
import { listJobs, companies, PAGE_SIZE } from "@/lib/jobs";
import { filtersHref, parseFilters, type SearchParams } from "@/lib/filters";
import { STATUS_LABELS } from "@/lib/model";
import { JobCard } from "@/components/job-card";
import { JobFilters } from "@/components/job-filters";
export default async function JobsPage({
  searchParams,
}: {
  searchParams: Promise<SearchParams>;
}) {
  const { client } = await requirePageSession();
  const filters = parseFilters(await searchParams);
  const [result, names] = await Promise.all([
    listJobs(client, filters),
    companies(client),
  ]);
  const now = new Date().toISOString();
  const threshold = silentThreshold();
  return (
    <>
      <nav className="summary-grid" aria-label="Application pipeline">
        {(
          [
            "new",
            "applied",
            "interview",
            "rejected",
            "silent",
            "offer",
          ] as const
        ).map((s) => (
          <Link
            className={`summary-card${filters.status === s ? " active" : ""}`}
            key={s}
            href={filtersHref(filters, {
              status: filters.status === s ? "all" : s,
              page: 1,
            })}
          >
            <strong>{result.counts[s]}</strong>
            <span>{STATUS_LABELS[s]}</span>
          </Link>
        ))}
      </nav>
      <JobFilters filters={filters} companies={names} />
      {(filters.fit === "relevant" || filters.fit === "maybe") && (
        <p className="notice">
          Check-fit roles may require more experience or a different engineering
          specialty. Review their requirements before applying. Default resume
          tailoring processes included jobs only.
        </p>
      )}
      <div className="toolbar">
        <span>
          {filters.notification ? "Most recent notification" : "Your job radar"}
        </span>
        <span>
          {result.total
            ? `Showing ${(filters.page - 1) * PAGE_SIZE + 1}–${Math.min(filters.page * PAGE_SIZE, result.total)} of ${result.total}`
            : "0 matching jobs"}
        </span>
      </div>
      {result.jobs.length ? (
        <div className="job-list">
          {result.jobs.map((job) => (
            <JobCard
              key={job.id}
              initial={job}
              now={now}
              silentAfter={threshold}
            />
          ))}
        </div>
      ) : (
        <section className="empty">
          <h2>
            {filters.notification
              ? "No jobs match this notification view"
              : "No jobs match these filters"}
          </h2>
          <p className="notice">
            Try including “check fit” roles or showing backfill jobs. Closed
            roles and backfill are hidden by default.
          </p>
          {filters.notification && (
            <p className="notice">
              This view requires a recorded notification. Suppressed backfill
              timestamps do not count as sent emails.
            </p>
          )}
          <Link className="button" href="/jobs?fit=relevant&backfill=1">
            Review available roles
          </Link>
        </section>
      )}
      {result.total > PAGE_SIZE && (
        <nav className="pagination" aria-label="Pagination">
          {filters.page > 1 ? (
            <Link
              className="button small"
              href={filtersHref(filters, { page: filters.page - 1 })}
            >
              Previous
            </Link>
          ) : (
            <span />
          )}
          <span className="notice">Page {filters.page}</span>
          {filters.page * PAGE_SIZE < result.total ? (
            <Link
              className="button small"
              href={filtersHref(filters, { page: filters.page + 1 })}
            >
              Next
            </Link>
          ) : (
            <span />
          )}
        </nav>
      )}
    </>
  );
}
