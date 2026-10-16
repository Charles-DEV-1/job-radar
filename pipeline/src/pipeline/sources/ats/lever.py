from typing import Any

from bs4 import BeautifulSoup

from pipeline.models import ProbeResult, RawJob
from pipeline.sources.ats.base import ATSAdapter


class LeverAdapter(ATSAdapter):
    name = "lever"
    collection_key = None

    def endpoint(self, slug: str) -> str:
        return f"https://api.lever.co/v0/postings/{self.safe_slug(slug)}?mode=json"

    def _identity(self, payload: Any, jobs: list[RawJob], result: ProbeResult) -> None:
        super()._identity(payload, jobs, result)
        for job in jobs:
            # Check every posting, not just the three diagnostic samples. Keep
            # descriptions only in memory; Detector excludes this field from logs.
            for key in (
                "applyUrl",
                "descriptionPlain",
                "description",
                "descriptionBodyPlain",
                "descriptionBody",
                "openingPlain",
                "opening",
                "additionalPlain",
                "additional",
            ):
                value = job.get(key)
                if isinstance(value, str) and value.strip():
                    result.job_identity_texts.append(
                        BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
                        if "<" in value
                        else value
                    )
