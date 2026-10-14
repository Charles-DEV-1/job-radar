"use client";
export default function ErrorPage({
  reset,
}: {
  error: Error;
  reset: () => void;
}) {
  return (
    <section className="empty">
      <h2>Jobs could not be loaded</h2>
      <p className="notice">
        Please try again. If this continues, check the dashboard configuration
        and database migration.
      </p>
      <button className="button" onClick={reset}>
        Try again
      </button>
    </section>
  );
}
