"use client";
import { useState } from "react";
export function ResumeDownload({
  id,
  status,
}: {
  id: string;
  status: "pending" | "ready" | "failed" | "archived" | "low_fit";
}) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (status === "archived")
    return <span className="notice">Resume archived after 30 days</span>;
  async function download() {
    setPending(true);
    setError(null);
    try {
      const response = await fetch(`/api/jobs/${id}/resume`, {
        cache: "no-store",
      });
      const result = await response.json();
      if (!response.ok || !result.url)
        throw new Error(
          result.error || "Download unavailable. Please try again.",
        );
      const link = document.createElement("a");
      link.href = result.url;
      link.download = result.filename;
      link.rel = "noreferrer";
      link.click();
      if (result.kind === "master" && status === "ready")
        setError("Tailored file unavailable; downloading your master resume.");
    } catch (e) {
      setError(
        e instanceof Error
          ? e.message
          : "Download unavailable. Please try again.",
      );
    } finally {
      setPending(false);
    }
  }
  return (
    <span>
      <button className="button" onClick={download} disabled={pending}>
        {pending
          ? "Preparing…"
          : status === "ready"
            ? "Download resume"
            : "Download master"}
      </button>
      {error && (
        <span role="alert" className="error" style={{ display: "block" }}>
          {error}
        </span>
      )}
    </span>
  );
}
