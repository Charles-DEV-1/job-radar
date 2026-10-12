"""Incremental page extraction; only real, permitted links become jobs."""

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from pipeline.llm.client import ExtractionClient, Posting, Usage
from pipeline.models import Job, SourceConfig
from pipeline.sources.pages import (
    PageFetcher,
    UnsafeRedirect,
    changed_context,
    clean_url,
    permitted_domain,
    visible_text,
)
from pipeline.storage import job_record


def posting_job(
    posting: Posting, source: SourceConfig, observed: datetime, description: str = ""
) -> Job:
    return Job(
        source=source.ats,
        source_company_slug=source.slug,
        external_id=hashlib.sha256(posting.apply_url.encode()).hexdigest(),
        company=source.company_name,
        title=posting.title.strip(),
        location_raw=posting.location or "",
        location_tags=[],
        remote_flag=False,
        department=None,
        employment_type=None,
        description_text=description or posting.description_snippet or "",
        description_html="",
        apply_url=posting.apply_url,
        posted_at=None,
        first_seen_at=observed,
        source_type=source.source_type,
    )


def cached_jobs(state: dict[str, Any]) -> list[Job]:
    jobs: list[Job] = []
    for record in state.get("jobs", []):
        row = dict(record)
        row["first_seen_at"] = datetime.fromisoformat(row["first_seen_at"])
        row["posted_at"] = (
            datetime.fromisoformat(row["posted_at"]) if row["posted_at"] else None
        )
        jobs.append(Job(**row))
    return jobs


class CareersPageAdapter:
    def __init__(self, pages: PageFetcher, llm: ExtractionClient | None = None) -> None:
        self.pages, self.llm = pages, llm

    def fetch(
        self,
        source: SourceConfig,
        previous: dict[str, Any],
        observed: datetime,
        usage: Usage,
    ) -> tuple[list[Job], dict[str, Any], list[str]]:
        page, method = self.pages.fetch(
            source.url, previous.get("method"), source.company_domain
        )
        old_text = visible_text("\n".join(previous.get("lines", [])))
        if len(old_text) >= 200 and len(page.visible_text) < len(old_text) * 0.4:
            raise ValueError(
                "Page snapshot shrank dramatically; previous snapshot retained"
            )
        state = {**page.record(), "method": method}
        jobs = {job.external_id: job for job in cached_jobs(previous)}
        warnings = list(page.warnings)
        if previous.get("hash") == page.content_hash:
            state["jobs"] = previous.get("jobs", [])
            return list(jobs.values()), state, warnings
        content = changed_context(previous.get("lines", []), page)
        if content:
            llm = self.llm or ExtractionClient.from_environment(self.pages.http)
            for posting in llm.extract(content, usage):
                # Require an absolute URL before canonicalizing tracking noise.
                parsed = urlsplit(posting.apply_url)
                try:
                    url = clean_url(posting.apply_url)
                except ValueError:
                    url = ""
                if (
                    not posting.title.strip()
                    or parsed.scheme not in {"http", "https"}
                    or url not in page.links
                    or not permitted_domain(url, source.company_domain)
                ):
                    warnings.append(
                        "Rejected posting: empty title or unverified apply URL"
                    )
                    continue
                posting = posting.model_copy(update={"apply_url": url})
                description = ""
                if url != clean_url(source.url):
                    try:
                        detail, _ = self.pages.fetch(url, domain=source.company_domain)
                        description = detail.text
                        warnings.extend(detail.warnings)
                    except UnsafeRedirect:
                        warnings.append(
                            "Rejected posting: apply URL redirects outside "
                            "permitted domains"
                        )
                        continue
                    except Exception as exc:
                        warnings.append(
                            "Detail page unavailable (" + type(exc).__name__ + ")"
                        )
                job = posting_job(posting, source, observed, description)
                jobs[job.external_id] = job
        # A diff cannot prove closure. Retain previously discovered postings.
        state["jobs"] = [job_record(job) for job in jobs.values()]
        return list(jobs.values()), state, warnings
