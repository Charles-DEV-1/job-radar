"""Concurrent ATS, careers-page and explicitly verified feed fetching."""

import argparse
import hashlib
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
from dotenv import load_dotenv
from ruamel.yaml.error import YAMLError

from pipeline.companies import CompanyFile
from pipeline.dedupe import hash_job
from pipeline.detect_ats import REPO_ROOT, readable
from pipeline.detector import RunRecorder
from pipeline.filters import JobFilter
from pipeline.http import HTTPClient
from pipeline.llm.client import Usage
from pipeline.models import ATSName, SourceConfig, SourceOutcome
from pipeline.normalize import normalize_jobs
from pipeline.sources.ats import create_adapters
from pipeline.sources.ats.base import ATSAdapter
from pipeline.sources.careers_page import CareersPageAdapter
from pipeline.sources.feed import FeedAdapter, load_feeds
from pipeline.sources.pages import PageFetcher, clean_url
from pipeline.storage import Store, StoreError, SupabaseStore, job_record


def load_sources(path: Path, adapters: dict[str, ATSAdapter]) -> list[SourceConfig]:
    company_file = CompanyFile(path)
    sources: list[SourceConfig] = []
    seen: set[tuple[str, str]] = set()
    for company, entry in zip(
        company_file.companies, company_file.document["companies"], strict=True
    ):
        confirmed_with_jobs = (
            entry.get("detection_status") == "confirmed"
            and entry.get("ats") in adapters
            and (entry.get("ats_job_count") or 0) > 0
        )
        if company.careers_url and not confirmed_with_jobs:
            url = clean_url(company.careers_url)
            sources.append(
                SourceConfig(
                    company.name,
                    "careers_page",
                    hashlib.sha256((company.domain + "|" + url).encode()).hexdigest(),
                    url,
                    company.enabled,
                    company.domain,
                    "careers_page",
                )
            )
        if entry.get("detection_status") != "confirmed":
            continue
        ats, slug = entry.get("ats"), entry.get("ats_slug")
        if ats not in adapters or not isinstance(slug, str) or not slug:
            raise ValueError(
                f"{company.name}: confirmed ATS requires a supported ATS and slug"
            )
        key = (ats, slug)
        if key in seen:
            raise ValueError("The same ATS board is configured more than once")
        seen.add(key)
        sources.append(
            SourceConfig(
                company.name,
                cast(ATSName, ats),
                slug,
                adapters[ats].endpoint(slug),
                company.enabled,
            )
        )
    return sources


def safe_error(exc: Exception) -> str:
    if isinstance(exc, StoreError):
        return str(exc)
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} from public source"
    if isinstance(exc, ValueError):
        # Our validators emit field names, never descriptions or credentials.
        return str(exc)
    return type(exc).__name__


def fetch_one(
    source: SourceConfig,
    source_id: str,
    adapter: ATSAdapter | CareersPageAdapter | FeedAdapter,
    store: Store,
    filters: JobFilter,
    recorder: RunRecorder,
) -> SourceOutcome:
    run_id = str(uuid4())
    started_at, started = datetime.now(UTC), time.monotonic()
    jobs_found = 0
    usage = Usage()
    warnings: list[str] = []
    monitor = isinstance(adapter, (CareersPageAdapter, FeedAdapter))
    try:
        store.start_run(source_id, run_id, started_at)
        observed = datetime.now(UTC)
        state: dict[str, Any] = {}
        previous: dict[str, Any] = {}
        if isinstance(adapter, (CareersPageAdapter, FeedAdapter)):
            previous = store.monitor_state(source_id)
            normalized, state, warnings = adapter.fetch(
                source, previous, observed, usage
            )
            jobs_found = len(normalized)
        else:
            raw_jobs = adapter.fetch_jobs(source.slug)
            jobs_found = len(raw_jobs)
            normalized = normalize_jobs(raw_jobs, source, observed)
        jobs = [hash_job(filters.classify(job)) for job in normalized]
        duration = max(0, round((time.monotonic() - started) * 1000))
        if monitor:
            state["jobs"] = [job_record(job) for job in jobs]
            counts = store.complete_monitor(
                run_id,
                jobs,
                observed,
                duration,
                state,
                previous.get("hash"),
                {"llm": usage.record(), "warnings": warnings},
            )
        else:
            counts = store.complete_run(run_id, jobs, observed, duration)
        result = SourceOutcome(source, "ok", counts=counts)
    except Exception as exc:
        error = safe_error(exc)
        duration = max(0, round((time.monotonic() - started) * 1000))
        try:
            store.fail_run(run_id, error, duration, datetime.now(UTC), jobs_found)
            if monitor:
                store.run_metadata(
                    run_id, {"llm": usage.record(), "warnings": warnings}
                )
        except Exception as record_error:
            error += "; Supabase run recording failed: " + safe_error(record_error)
        result = SourceOutcome(source, "error", error=error)
    # Durable fallback also covers a database outage; never write raw jobs here.
    try:
        recorder.write(
            {
                "event": "fetch",
                "source_run_id": run_id,
                "company": source.company_name,
                "ats": source.ats,
                "source_type": source.source_type,
                "slug": source.slug,
                "status": result.status,
                "jobs_found": jobs_found,
                "duration_ms": duration,
                "error": result.error,
                "counts": asdict(result.counts) if result.counts else None,
                "llm": usage.record(),
                "warnings": warnings,
            }
        )
    except OSError:
        result.status = "error"
        result.error = (
            result.error + "; " if result.error else ""
        ) + "Local run recording failed"
    return result


def run_sources(
    sources: list[SourceConfig],
    adapters: dict[str, ATSAdapter | CareersPageAdapter | FeedAdapter],
    store: Store,
    filters: JobFilter,
    recorder: RunRecorder,
    concurrency: int = 4,
) -> list[SourceOutcome]:
    if not 1 <= concurrency <= 16:
        raise ValueError("Concurrency must be between 1 and 16")
    if len({(source.ats, source.slug) for source in sources}) != len(sources):
        raise ValueError("The same source is configured more than once")
    ids = store.sync_sources(sources)
    enabled = [source for source in sources if source.enabled]
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(
                fetch_one,
                source,
                ids[(source.ats, source.slug)],
                adapters[source.ats],
                store,
                filters,
                recorder,
            )
            for source in enabled
        ]
        return [future.result() for future in futures]


def print_summary(outcomes: list[SourceOutcome]) -> None:
    rows = [
        (
            "Company",
            "Source",
            "Status",
            "Found",
            "Stored",
            "New",
            "Changed",
            "Duplicates",
            "Closed",
            "Backfill",
        )
    ]
    for outcome in outcomes:
        count = outcome.counts
        rows.append(
            (
                outcome.source.company_name,
                outcome.source.ats,
                outcome.status,
                *[
                    str(getattr(count, field)) if count else "-"
                    for field in (
                        "jobs_found",
                        "inserted_jobs",
                        "new_jobs",
                        "updated_jobs",
                        "duplicates",
                        "closed_jobs",
                        "backfill",
                    )
                ],
            )
        )
    widths = [max(len(readable(row[i])) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        print(
            " | ".join(readable(value).ljust(widths[i]) for i, value in enumerate(row))
        )
    new_jobs = sum(outcome.counts.new_jobs for outcome in outcomes if outcome.counts)
    print(f"New notifiable jobs: {new_jobs}")
    for outcome in outcomes:
        if outcome.error:
            print(f"{readable(outcome.source.company_name)}: {readable(outcome.error)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fetch ATS, careers-page and verified feed sources "
        "into private Supabase"
    )
    parser.add_argument(
        "--companies-file", type=Path, default=REPO_ROOT / "data/companies.yaml"
    )
    parser.add_argument(
        "--filters-file", type=Path, default=REPO_ROOT / "data/filters.yaml"
    )
    parser.add_argument(
        "--report-file", type=Path, default=REPO_ROOT / "data/fetch-runs.jsonl"
    )
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--feeds-file", type=Path, default=REPO_ROOT / "data/feeds.yaml"
    )
    args = parser.parse_args(argv)
    store: SupabaseStore | None = None
    try:
        load_dotenv(REPO_ROOT / ".env", override=False)
        with HTTPClient() as http:
            adapters = {adapter.name: adapter for adapter in create_adapters(http)}
            sources = load_sources(args.companies_file, adapters)
            sources.extend(load_feeds(args.feeds_file))
            pages = PageFetcher(http)
            adapters["careers_page"] = CareersPageAdapter(pages)
            adapters["feed"] = FeedAdapter(pages)
            if not any(source.enabled for source in sources):
                print(
                    "No confirmed ATS entries or eligible careers/feed sources "
                    "configured."
                )
                return 0
            filters = JobFilter(args.filters_file)
            store = SupabaseStore.from_environment(http)
            outcomes = run_sources(
                sources,
                adapters,
                store,
                filters,
                RunRecorder(args.report_file),
                args.concurrency,
            )
            print_summary(outcomes)
            return 1 if any(outcome.status == "error" for outcome in outcomes) else 0
    except (OSError, ValueError, KeyError, YAMLError, StoreError) as exc:
        print("Fetch setup failed: " + readable(safe_error(exc)))
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
