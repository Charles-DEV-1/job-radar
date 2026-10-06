from typing import Any

from pipeline.models import RawJob
from pipeline.sources.ats.base import ATSAdapter


class SmartRecruitersAdapter(ATSAdapter):
    name = "smartrecruiters"
    collection_key = "content"

    def endpoint(self, slug: str) -> str:
        return f"https://api.smartrecruiters.com/v1/companies/{self.safe_slug(slug)}/postings"

    def _count(self, payload: Any, jobs: list[RawJob]) -> int:
        total = payload.get("totalFound")
        if type(total) is not int or total < len(jobs) or (total > 0 and not jobs):
            raise ValueError("Invalid SmartRecruiters totalFound or empty partial page")
        return total

    def fetch_jobs(self, slug: str) -> list[RawJob]:
        url = self.endpoint(slug)
        payload = self._payload(url)
        jobs = self._jobs(payload)
        total = self._count(payload, jobs)
        while len(jobs) < total:
            offset = len(jobs)
            page = self._payload(f"{url}?offset={offset}&limit=100")
            batch = self._jobs(page)
            if self._count(page, batch) != total:
                raise ValueError(
                    "SmartRecruiters total changed during pagination; retry"
                )
            if page.get("offset") != offset or not batch:
                raise ValueError("SmartRecruiters pagination did not advance")
            if len(jobs) + len(batch) > total:
                raise ValueError(
                    "SmartRecruiters total changed during pagination; retry"
                )
            jobs.extend(batch)
            if len(jobs) >= 10000 and len(jobs) < total:
                raise ValueError("SmartRecruiters pagination limit exceeded")
        return jobs
