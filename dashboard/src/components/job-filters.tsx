import { type Filters } from "@/lib/filters";
import { STATUSES, STATUS_LABELS } from "@/lib/model";
export function JobFilters({
  filters,
  companies,
}: {
  filters: Filters;
  companies: string[];
}) {
  return (
    <form action="/jobs" method="get">
      {filters.notification && <input type="hidden" name="new" value="1" />}
      <div className="filters">
        <label className="field search">
          Search
          <input
            name="q"
            defaultValue={filters.q}
            placeholder="Title or company"
            maxLength={100}
          />
        </label>
        <label className="field">
          Fit
          <select name="fit" defaultValue={filters.fit}>
            <option value="include">Included roles</option>
            <option value="relevant">Include “check fit” roles</option>
            <option value="maybe">Check fit only</option>
            <option value="exclude">Excluded roles</option>
          </select>
        </label>
        <button className="button primary">Search</button>
      </div>
      <details
        className="filter-more"
        open={
          !!(
            filters.company ||
            filters.status !== "all" ||
            filters.from ||
            filters.to ||
            filters.closed ||
            filters.backfill
          )
        }
      >
        <summary>Filters &amp; visibility</summary>
        <div className="filters">
          <label className="field">
            Company
            <select name="company" defaultValue={filters.company}>
              <option value="">All companies</option>
              {companies.map((c) => (
                <option key={c}>{c}</option>
              ))}
            </select>
          </label>
          <label className="field">
            Status
            <select name="status" defaultValue={filters.status}>
              <option value="all">All statuses</option>
              {STATUSES.map((s) => (
                <option value={s} key={s}>
                  {STATUS_LABELS[s]}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            First seen from
            <input type="date" name="from" defaultValue={filters.from} />
          </label>
          <label className="field">
            Through
            <input type="date" name="to" defaultValue={filters.to} />
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              name="closed"
              value="1"
              defaultChecked={filters.closed}
            />
            Show closed
          </label>
          <label className="checkbox">
            <input
              type="checkbox"
              name="backfill"
              value="1"
              defaultChecked={filters.backfill}
            />
            Show backfill
          </label>
        </div>
      </details>
    </form>
  );
}
