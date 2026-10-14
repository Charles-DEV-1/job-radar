import Link from "next/link";
import { notFound } from "next/navigation";
import { z } from "zod";
import { requirePageSession } from "@/lib/auth";
import { silentThreshold } from "@/lib/config";
import { getJob, statusHistory } from "@/lib/jobs";
import { STATUS_LABELS } from "@/lib/model";
import { JobCard } from "@/components/job-card";
export default async function DetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { client } = await requirePageSession();
  const { id } = await params;
  if (!z.uuid().safeParse(id).success) notFound();
  const [job, history] = await Promise.all([
    getJob(client, id),
    statusHistory(client, id),
  ]);
  if (!job) notFound();
  return (
    <>
      <Link className="button small" href="/jobs">
        ← All jobs
      </Link>
      <div className="job-list" style={{ marginTop: 20 }}>
        <JobCard
          initial={job}
          now={new Date().toISOString()}
          silentAfter={silentThreshold()}
        />
      </div>
      <div className="detail-grid">
        <section className="panel">
          <h2>Job description</h2>
          <p className="description">
            {job.description_text ||
              "No full description is available. Open the apply link to review the listing."}
          </p>
        </section>
        <aside style={{ display: "grid", gap: 20, alignContent: "start" }}>
          <section className="panel">
            <h2>Match notes</h2>
            {job.match_notes ? (
              <>
                <h3>Matched requirements</h3>
                {job.match_notes.matched.length ? (
                  <ul>
                    {job.match_notes.matched.map((m, i) => (
                      <li key={i}>{m.requirement}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="notice">No matched requirements recorded.</p>
                )}
                <h3>Gaps</h3>
                {job.match_notes.gaps.length ? (
                  <ul>
                    {job.match_notes.gaps.map((g, i) => (
                      <li key={i}>{g}</li>
                    ))}
                  </ul>
                ) : (
                  <p className="notice">
                    No gaps recorded. This does not confirm every requirement is
                    covered.
                  </p>
                )}
              </>
            ) : (
              <p className="notice">
                Match notes are available after successful tailoring.
              </p>
            )}
            <details>
              <summary className="notice">Why this classification?</summary>
              <p className="notice">{job.filter_reason}</p>
            </details>
            {job.resume_error && (
              <p className="error">Tailoring failed: {job.resume_error}</p>
            )}
          </section>
          <section className="panel">
            <h2>Status history</h2>
            {history.length ? (
              <ol className="history">
                {history.map((event) => (
                  <li key={event.id}>
                    <span>{STATUS_LABELS[event.status]}</span>
                    <time dateTime={event.changed_at}>
                      {new Date(event.changed_at).toLocaleString("en-GB", {
                        timeZone: "Africa/Lagos",
                        dateStyle: "medium",
                        timeStyle: "short",
                      })}
                    </time>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="notice">
                No status changes recorded yet. Earlier transition dates are
                unknown.
              </p>
            )}
          </section>
        </aside>
      </div>
    </>
  );
}
