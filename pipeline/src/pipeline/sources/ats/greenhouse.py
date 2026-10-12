from pipeline.models import RawJob
from pipeline.sources.ats.base import ATSAdapter


class GreenhouseAdapter(ATSAdapter):
    name = "greenhouse"

    def endpoint(self, slug: str) -> str:
        return f"https://boards-api.greenhouse.io/v1/boards/{self.safe_slug(slug)}/jobs"

    def fetch_jobs(self, slug: str) -> list[RawJob]:
        payload = self._payload(self.endpoint(slug) + "?content=true")
        jobs = self._jobs(payload)
        meta = payload.get("meta")
        if (
            not isinstance(meta, dict)
            or type(meta.get("total")) is not int
            or meta["total"] != len(jobs)
        ):
            raise ValueError("Greenhouse returned an incomplete jobs snapshot")
        return jobs
