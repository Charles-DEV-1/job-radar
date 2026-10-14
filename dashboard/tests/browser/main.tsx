// Test-only visual harness. This is outside src/app and is never a public route.
import { createRoot } from "react-dom/client";
import { JobCard } from "../../src/components/job-card";
import { Brand } from "../../src/components/brand";
import { job } from "../fixtures";
import "../../src/app/globals.css";
const now = "2026-10-25T12:00:00Z";
const jobs = [
  job(),
  job({
    id: "00000000-0000-4000-8000-000000000002",
    title: "Software Engineering Intern",
    status: "applied",
  }),
  job({
    id: "00000000-0000-4000-8000-000000000003",
    title: "Platform Engineer",
    resume_status: "failed",
    filter_result: "maybe",
    status: "applied",
  }),
  job({
    id: "00000000-0000-4000-8000-000000000004",
    title: "Backend Engineer (Python)",
    status: "interview",
  }),
];
createRoot(document.getElementById("root")!).render(
  <>
    <header className="topbar">
      <div className="shell topbar-inner">
        <Brand />
        <div className="top-actions">
          <span className="last-check">Component test</span>
          <button className="button small">Sign out</button>
        </div>
      </div>
    </header>
    <main className="shell main">
      <nav className="summary-grid">
        {["New", "Applied", "Interview", "Rejected", "Silent", "Offer"].map(
          (s, i) => (
            <a
              href="#"
              className={`summary-card${i === 0 ? " active" : ""}`}
              key={s}
            >
              <strong>{i === 0 ? 2 : i === 1 ? 2 : 0}</strong>
              <span>{s}</span>
            </a>
          ),
        )}
      </nav>
      <div className="filters">
        <label className="field search">
          Search
          <input placeholder="Title or company" />
        </label>
        <label className="field">
          Fit
          <select>
            <option>Include “check fit” roles</option>
          </select>
        </label>
        <button className="button primary">Search</button>
      </div>
      <div className="job-list">
        {jobs.map((j) => (
          <JobCard key={j.id} initial={j} now={now} silentAfter={21} />
        ))}
      </div>
    </main>
  </>,
);
