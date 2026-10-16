"""Run with python -m pipeline.detect_ats; writes company fields only with --write."""

import argparse
import sys
from pathlib import Path

from ruamel.yaml.error import YAMLError

from pipeline.companies import CompanyFile
from pipeline.detector import Detector, RunRecorder
from pipeline.discovery import CareersDiscovery
from pipeline.http import HTTPClient
from pipeline.models import DetectionResult
from pipeline.sources.ats import create_adapters

REPO_ROOT = Path(__file__).resolve().parents[3]


def readable(value: object) -> str:
    # Keep untrusted company/job text from emitting terminal control sequences.
    return "".join(char if char.isprintable() else " " for char in str(value))


def print_report(results: list[DetectionResult]) -> None:
    headers = ("Company", "ATS", "Slug", "Status", "Jobs")
    rows = [
        (
            r.company.name,
            r.ats,
            r.ats_slug or "-",
            r.detection_status,
            str(r.job_count) if r.job_count is not None else "-",
        )
        for r in results
    ]
    widths = [max(len(readable(row[i])) for row in [headers, *rows]) for i in range(5)]
    for index, row in enumerate([headers, *rows]):
        print(
            " | ".join(readable(value).ljust(widths[i]) for i, value in enumerate(row))
        )
        if index == 0:
            print("-+-".join("-" * width for width in widths))
    for result in results:
        if result.job_count == 0 and result.detection_status == "confirmed":
            print(f"{readable(result.company.name)}: 0 open jobs (empty, verify)")
        for match in result.matches:
            if match.status == "confirmed" and match.probe.job_count == 0:
                print(
                    f"{readable(result.company.name)}: {match.probe.ats}/"
                    f"{readable(match.probe.slug)}: empty, verify"
                )
    reviews = [
        (result, match)
        for result in results
        for match in result.matches
        if match.status == "needs_review"
    ]
    pending = [
        r for r in results if r.detection_status == "needs_review" and not r.matches
    ]
    if reviews or pending:
        print("\nNeeds your review:")
        for result, match in reviews:
            probe = match.probe
            print(
                f"- {readable(result.company.name)}: "
                f"{probe.ats}/{readable(probe.slug)}; "
                f"{readable(match.reason)}; {probe.job_count} open jobs"
            )
            print(
                "  Board company: "
                + readable(
                    ", ".join(probe.company_names + probe.company_domains)
                    or "not supplied"
                )
            )
            print(
                "  Sample titles: " + readable("; ".join(probe.sample_titles) or "none")
            )
            print(
                "  Sample apply URLs: "
                + readable("; ".join(probe.sample_apply_urls) or "none")
            )
        for result in pending:
            print(f"- {readable(result.company.name)}: {readable(result.reason)}")
    failures = [result for result in results if result.errors]
    if failures:
        print("\nSource/page errors (other sources continued):")
        for result in failures:
            for error in result.errors:
                print(f"- {readable(result.company.name)}: {readable(error)}")
    live = sum(r.detection_status == "confirmed" and bool(r.job_count) for r in results)
    empty = sum(r.detection_status == "confirmed" and r.job_count == 0 for r in results)
    print(
        "\nDetection summary: "
        f"confirmed with jobs: {live}; "
        f"confirmed but empty: {empty}; "
        f"needs review: {sum(r.detection_status == 'needs_review' for r in results)}; "
        f"not found: {sum(r.detection_status == 'not_found' for r in results)}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect and verify public company ATS boards"
    )
    parser.add_argument("--company", help="Select one enabled company by name or alias")
    parser.add_argument(
        "--write", action="store_true", help="Update YAML detector fields in place"
    )
    parser.add_argument(
        "--companies-file", type=Path, default=REPO_ROOT / "data/companies.yaml"
    )
    parser.add_argument(
        "--report-file", type=Path, default=REPO_ROOT / "data/detection-runs.jsonl"
    )
    args = parser.parse_args(argv)
    try:
        company_file = CompanyFile(args.companies_file)
        companies = [company for company in company_file.companies if company.enabled]
        if args.company:
            companies = [
                company
                for company in companies
                if args.company.casefold()
                in {name.casefold() for name in (company.name, *company.aliases)}
            ]
            if not companies:
                parser.error("No enabled company matches the supplied name or alias")
            if len(companies) > 1:
                parser.error("Alias matches multiple companies; use the display name")
        if not companies:
            print(
                "No enabled companies. The template examples are disabled; "
                "add your company list."
            )
            return 0
        recorder = RunRecorder(args.report_file)
        with HTTPClient() as http:
            detector = Detector(create_adapters(http), CareersDiscovery(http), recorder)
            results = [detector.detect(company) for company in companies]
        print_report(results)
        if args.write:
            company_file.write(results)
            print(f"\nUpdated {args.companies_file}")
        print(f"Run records: {args.report_file}")
        return 1 if any(result.errors for result in results) else 0
    except (OSError, ValueError, YAMLError) as exc:
        print(f"ATS detection failed: {readable(exc)}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
