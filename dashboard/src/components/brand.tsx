import Link from "next/link";
export function Brand() {
  return (
    <Link href="/jobs" className="brand">
      <svg
        aria-hidden="true"
        width="24"
        height="24"
        viewBox="0 0 24 24"
        fill="none"
        stroke="#117b60"
        strokeWidth="2"
      >
        <circle cx="12" cy="12" r="8" />
        <circle cx="12" cy="12" r="4" />
        <path d="m12 12 7-8" />
      </svg>
      Job Radar
    </Link>
  );
}
