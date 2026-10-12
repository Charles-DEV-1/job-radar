from pipeline.models import RawJob
from pipeline.sources.ats.base import ATSAdapter


class WorkableAdapter(ATSAdapter):
    name = "workable"
    board_name_key = "name"

    def fetch_jobs(self, slug: str) -> list[RawJob]:
        # The documented www.workable.com endpoint redirects to this verified URL.
        url = self.endpoint(slug) + "?details=true"
        return self._jobs(self._payload(url))

    def endpoint(self, slug: str) -> str:
        return (
            f"https://apply.workable.com/api/v1/widget/accounts/{self.safe_slug(slug)}"
        )
