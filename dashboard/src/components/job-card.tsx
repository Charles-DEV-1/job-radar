"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  jobSchema,
  STATUSES,
  STATUS_LABELS,
  relativeTime,
  safeApplyUrl,
  silentDays,
  type Job,
  type JobStatus,
} from "@/lib/model";
import { ResumeDownload } from "./resume-download";

export function JobCard({
  initial,
  now,
  silentAfter,
}: {
  initial: Job;
  now: string;
  silentAfter: number;
}) {
  const [job, setJob] = useState(initial);
  const [draft, setDraft] = useState(initial.notes ?? "");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const router = useRouter();
  useEffect(() => {
    setJob(initial);
    setDraft(initial.notes ?? "");
  }, [initial]);
  async function update(values: { status?: JobStatus; notes?: string }) {
    setPending(true);
    setError(null);
    setSaved(false);
    try {
      const response = await fetch(`/api/jobs/${job.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...values, expected_status: job.status }),
      });
      const result = await response.json();
      if (!response.ok)
        throw new Error(result.error || "Could not save. Please try again.");
      setJob(jobSchema.parse(result.job));
      if (values.notes !== undefined) setSaved(true);
      router.refresh();
    } catch (e) {
      setError(
        e instanceof Error ? e.message : "Could not save. Please try again.",
      );
    } finally {
      setPending(false);
    }
  }
  const days = silentDays(job, now, silentAfter);
  const apply = safeApplyUrl(job.apply_url);
  return (
    <article
      className={`job-card${days !== null ? " suggested" : ""}`}
      aria-label={`${job.company}: ${job.title}`}
    >
      <div className="job-main">
        <div>
          <Link href={`/jobs/${job.id}`} className="job-title">
            {job.title}
          </Link>
          <div className="job-meta">
            {job.company} · {job.location_raw || "Location unspecified"}
            {job.remote_flag ? " · Remote" : ""} · posted{" "}
            {relativeTime(job.posted_at ?? job.first_seen_at, now)} ·{" "}
            {job.resume_status === "archived"
              ? "Resume archived"
              : job.resume_status === "low_fit"
                ? "Low fit · master resume"
                : job.resume_status === "ready"
                  ? "Resume ready"
                  : job.resume_status === "failed"
                    ? "Master resume (tailoring failed)"
                    : "Master resume (not tailored yet)"}
          </div>
        </div>
        <div className="job-actions">
          <span
            className={`fit ${job.filter_result}`}
            title={job.filter_reason}
          >
            {job.filter_result === "include"
              ? "Include"
              : job.filter_result === "maybe"
                ? "Check fit"
                : "Excluded"}
          </span>
          <div className="primary-actions">
            {apply ? (
              <a
                className="button primary"
                href={apply}
                target="_blank"
                rel="noopener noreferrer"
              >
                Apply ↗
              </a>
            ) : (
              <span className="notice">Apply link unavailable</span>
            )}
            <ResumeDownload id={job.id} status={job.resume_status} />
          </div>
          <div
            className="status-buttons"
            role="group"
            aria-label="Application status"
          >
            {STATUSES.map((status) => (
              <button
                key={status}
                className={`status-button ${status}${job.status === status ? " selected" : ""}`}
                aria-pressed={job.status === status}
                disabled={pending || status === job.status}
                onClick={() => update({ status })}
              >
                {STATUS_LABELS[status]}
              </button>
            ))}
          </div>
        </div>
      </div>
      {days !== null && (
        <p className="silent-hint">
          Applied {days} days ago with no recorded status change. Mark as
          Silent?
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      <div className="job-footer">
        <span>{job.source}</span>
        <time
          dateTime={job.first_seen_at}
          title={new Date(job.first_seen_at).toLocaleString("en-GB", {
            timeZone: "Africa/Lagos",
          })}
        >
          First seen {relativeTime(job.first_seen_at, now)}
        </time>
        {job.backfill && <span>Backfill</span>}
        {job.closed_at && <span>Closed</span>}
        <details>
          <summary>Notes{job.notes ? " · saved" : ""}</summary>
          <div className="notes-editor">
            <label className="field" style={{ flex: 1 }}>
              Private notes
              <textarea
                className="notes-input"
                value={draft}
                onChange={(e) => {
                  setDraft(e.target.value);
                  setSaved(false);
                }}
                maxLength={5000}
              />
            </label>
            <button
              className="button small"
              disabled={pending}
              onClick={() => update({ notes: draft })}
            >
              {pending ? "Saving…" : "Save notes"}
            </button>
          </div>
          {saved && <p role="status">Notes saved.</p>}
        </details>
      </div>
    </article>
  );
}
